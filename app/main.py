"""FastAPI lifespan owns one Feishu child process. Run with one Uvicorn worker."""

import asyncio
from contextlib import asynccontextmanager
import multiprocessing
import logging
import sqlite3

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.db.connection import initialize
from app.integrations.feishu.listener import run_listener
from app.config import PROJECT_ROOT
from app.security.auth import LocalSessionMiddleware, bootstrap_admin, signing_key
from app.logging_config import add_file_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    initialize(settings.database_path)
    bootstrap_admin(settings)
    app.state.settings = settings
    app.state.signing_key = signing_key(settings.database_path.parent)
    log_handler = add_file_logging(settings, 'web')
    process = None
    app.state.feishu_process = None
    app.state.feishu_enabled = settings.feishu_enabled
    try:
        if settings.feishu_enabled:
            process = multiprocessing.get_context('spawn').Process(
                target=run_listener, args=(settings,), name='feishu-listener',
            )
            process.start()
            app.state.feishu_process = process
        yield
    finally:
        if process is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 5)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 5)
            process.close()
            app.state.feishu_process = None
        logging.getLogger('app').removeHandler(log_handler)
        log_handler.close()


app = FastAPI(title='发票小助手', lifespan=lifespan)
app.add_middleware(LocalSessionMiddleware)

from app.api import auth, invoices, titles, reimbursements, print_jobs, admin
for module in (auth, invoices, titles, reimbursements, print_jobs, admin):
    app.include_router(module.router)

app.mount('/static', StaticFiles(directory=PROJECT_ROOT / 'app' / 'web'), name='static')


@app.get('/resource/invoice-qr.jpg', include_in_schema=False)
def feishu_bot_qr():
    return FileResponse(PROJECT_ROOT / 'resource' / 'invoice-qr.jpg', media_type='image/jpeg')


@app.middleware('http')
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(status_code=422, content={'detail': [
        {'loc': list(error['loc']), 'msg': error['msg']} for error in exc.errors()
    ]})


@app.exception_handler(sqlite3.IntegrityError)
async def constraint_error(request, exc):
    return JSONResponse(status_code=409, content={'detail': '数据已存在或关联记录已变更，请刷新后重试'})


@app.get('/', include_in_schema=False)
@app.get('/login', include_in_schema=False)
@app.get('/invoices', include_in_schema=False)
@app.get('/titles', include_in_schema=False)
@app.get('/reimbursements', include_in_schema=False)
@app.get('/prints', include_in_schema=False)
@app.get('/settings', include_in_schema=False)
@app.get('/admin', include_in_schema=False)
def web_page():
    return FileResponse(PROJECT_ROOT / 'app' / 'web' / 'index.html')


@app.get('/health')
def health():
    if not app.state.feishu_enabled:
        return {'status': 'ok', 'feishu': 'disabled'}
    process = app.state.feishu_process
    if process is not None and process.is_alive():
        # Process liveness does not assert that the remote WebSocket is connected.
        return {'status': 'ok', 'feishu': 'process_running'}
    return JSONResponse(status_code=503, content={'status': 'degraded', 'feishu': 'process_stopped'})
