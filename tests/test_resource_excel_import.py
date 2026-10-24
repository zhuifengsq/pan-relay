import io
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from app import app
from src.services.resource_excel_service import ResourceExcelError, parse_resource_excel
from src.utils.auth_utils import create_jwt_token


def make_xlsx(rows):
    """Small inline-string workbook, matching the supplied headerless file."""
    body = []
    for row_number, cells in rows:
        cell_xml = "".join(
            f'<c r="{column}{row_number}" t="inlineStr"><is><t>{value}</t></is></c>'
            for column, value in cells.items()
        )
        body.append(f'<row r="{row_number}">{cell_xml}</row>')
    sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(body) + '</sheetData></worksheet>'
    output = io.BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook/>')
        archive.writestr('xl/worksheets/sheet1.xml', sheet)
    return output.getvalue()


class ResourceExcelImportTests(unittest.TestCase):
    def test_sample_file_shape_without_header(self):
        content = make_xlsx([
            (2, {'A': '璀璨之城', 'B': 'https://pan.baidu.com/s/abc?pwd=6666'}),
            (3, {'A': '璀璨之城', 'B': 'https://pan.quark.cn/s/xyz'}),
        ])
        rows, errors = parse_resource_excel(content)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['name'], '璀璨之城')
        self.assertEqual(rows[1]['share_link'], 'https://pan.quark.cn/s/xyz')

    def test_header_mapping_and_bad_rows(self):
        content = make_xlsx([
            (1, {'A': '分享链接', 'B': '资源标题', 'C': '备注'}),
            (2, {'A': 'https://pan.quark.cn/s/xyz', 'B': '测试资源', 'C': '4K'}),
            (3, {'A': 'not-a-link', 'B': '错误资源'}),
        ])
        rows, errors = parse_resource_excel(content)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['remarks'], '4K')
        self.assertEqual(len(errors), 1)
        self.assertIn('第 3 行', errors[0])

    def test_invalid_workbook(self):
        with self.assertRaises(ResourceExcelError):
            parse_resource_excel(b'not an excel file')

    def test_import_api_skips_duplicates_and_does_not_transfer(self):
        client = app.test_client()
        client.set_cookie('token', create_jwt_token())
        content = make_xlsx([
            (2, {'A': '资源一', 'B': 'https://pan.baidu.com/s/first'}),
            (3, {'A': '资源二', 'B': 'https://pan.quark.cn/s/second'}),
        ])
        with patch('src.routes.resource_routes.add_resource_and_share') as add:
            add.side_effect = [(True, '资源已直接入库', 1), (True, '资源链接已存在，无需重复导入', 2)]
            response = client.post(
                '/admin/api/resources/import-excel',
                data={'file': (io.BytesIO(content), 'resources.xlsx')},
                content_type='multipart/form-data',
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['imported'], 1)
        self.assertEqual(response.json['skipped'], 1)
        self.assertEqual(add.call_args_list[0].args[0]['origin_type'], 'batch_import')
        self.assertEqual(add.call_args_list[0].args[0]['save_to_netdisk'], {})


if __name__ == '__main__':
    unittest.main()
