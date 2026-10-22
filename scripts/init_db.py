"""Run with: python3 -m scripts.init_db --database data/app.db"""

import argparse
from pathlib import Path

from app.db.connection import initialize


def main() -> None:
    parser = argparse.ArgumentParser(description='Initialize the SQLite invoice database')
    parser.add_argument('--database', type=Path, default=Path('data/app.db'))
    args = parser.parse_args()
    initialize(args.database)
    print(f'Database initialized: {args.database.resolve()}')


if __name__ == '__main__':
    main()
