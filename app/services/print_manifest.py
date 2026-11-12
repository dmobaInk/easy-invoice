"""Write one reimbursement manifest row per printed invoice."""

import re

import pymupdf
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


def legacy_item_names(path):
    # Older records lack OCR details. Tax-classified item names in digital PDFs
    # start with *category* and occupy a separate line in the text layer.
    with pymupdf.open(path) as document:
        lines = document[0].get_text().splitlines()
    return ','.join(line.strip() for line in lines if re.match(r'^\s*\*[^*]+\*\S', line))


SHEET_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CONTENT_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.'


def add_row(table, number, values):
    row = ET.SubElement(table, 'row', r=str(number))
    for column, value in zip('ABCDE', values):
        cell = ET.SubElement(row, 'c', r=f'{column}{number}')
        if isinstance(value, str):
            cell.set('t', 'inlineStr')
            text = ET.SubElement(ET.SubElement(cell, 'is'), 't')
            text.text = re.sub(r'[^\x09\x0a\x0d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]', '', value)
        else:
            if column in 'DE':
                cell.set('s', '1')
            ET.SubElement(cell, 'v').text = str(value)


def generate_manifest(rows, target):
    sheet = ET.Element('worksheet', xmlns=SHEET_NS)
    columns = ET.SubElement(sheet, 'cols')
    for i, width in enumerate((52, 8, 36, 16, 16), 1):
        ET.SubElement(columns, 'col', min=str(i), max=str(i), width=str(width), customWidth='1')
    table = ET.SubElement(sheet, 'sheetData')
    add_row(table, 1, ('名称/型号', '数量', '公司', '单价', '总价（元）'))
    for index, (path, names, seller, amount) in enumerate(rows, 2):
        add_row(table, index, (names or legacy_item_names(path), 1, seller or '', amount, amount))

    workbook = ET.Element('workbook', {'xmlns': SHEET_NS, 'xmlns:r': OFFICE_REL})
    ET.SubElement(ET.SubElement(workbook, 'sheets'), 'sheet',
                  {'name': '报销清单', 'sheetId': '1', 'r:id': 'rId1'})
    root_rels = ET.Element('Relationships', xmlns=PACKAGE_REL)
    ET.SubElement(root_rels, 'Relationship', Id='rId1', Type=OFFICE_REL + '/officeDocument', Target='xl/workbook.xml')
    rels = ET.Element('Relationships', xmlns=PACKAGE_REL)
    ET.SubElement(rels, 'Relationship', Id='rId1', Type=OFFICE_REL + '/worksheet', Target='worksheets/sheet1.xml')
    ET.SubElement(rels, 'Relationship', Id='rId2', Type=OFFICE_REL + '/styles', Target='styles.xml')
    types = ET.Element('Types', xmlns=CONTENT_TYPES)
    ET.SubElement(types, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    for path, kind in [('xl/workbook.xml', 'sheet.main'), ('xl/worksheets/sheet1.xml', 'worksheet'), ('xl/styles.xml', 'styles')]:
        ET.SubElement(types, 'Override', PartName='/' + path, ContentType=MIME + kind + '+xml')

    styles = ET.fromstring(f'''<styleSheet xmlns="{SHEET_NS}">
        <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
        <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
        <borders count="1"><border/></borders>
        <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
        <cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
        <xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
        <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
        </styleSheet>''')
    with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
        for name, part in [('[Content_Types].xml', types), ('_rels/.rels', root_rels),
                           ('xl/workbook.xml', workbook), ('xl/_rels/workbook.xml.rels', rels),
                           ('xl/styles.xml', styles), ('xl/worksheets/sheet1.xml', sheet)]:
            archive.writestr(name, ET.tostring(part, encoding='utf-8', xml_declaration=True))
