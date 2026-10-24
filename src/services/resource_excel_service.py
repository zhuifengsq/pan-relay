"""Read resource lists from ordinary .xlsx files without a spreadsheet runtime."""

from io import BytesIO
import re
from urllib.parse import urlparse
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


NAMESPACE = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_ROWS = 1000
HEADER_ALIASES = {
    "name": {"标题", "资源标题", "资源名称", "名称", "文件名", "title", "name"},
    "share_link": {"链接", "分享链接", "网盘链接", "资源链接", "地址", "url", "link", "sharelink"},
    "type": {"类型", "分类", "type", "category"},
    "remarks": {"备注", "说明", "remark", "remarks", "note"},
}


class ResourceExcelError(ValueError):
    pass


def _column_index(reference):
    letters = re.match(r"[A-Z]+", reference or "")
    if not letters:
        return None
    result = 0
    for letter in letters.group():
        result = result * 26 + ord(letter) - ord("A") + 1
    return result - 1


def _cell_value(cell, shared_strings):
    if cell.get("t") == "inlineStr":
        return "".join(part.text or "" for part in cell.findall(".//m:is/m:t", NAMESPACE)).strip()
    value = cell.find("m:v", NAMESPACE)
    if value is None or value.text is None:
        return ""
    if cell.get("t") == "s":
        try:
            return shared_strings[int(value.text)].strip()
        except (ValueError, IndexError):
            raise ResourceExcelError("Excel 共享文本索引无效") from None
    return value.text.strip()


def _header_key(value):
    return re.sub(r"[\s_\-]+", "", value.strip().lower())


def _is_web_link(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc) and not any(ch.isspace() for ch in value)
    except ValueError:
        return False


def parse_resource_excel(file_bytes):
    """Return (resources, row_errors). Headerless sheets use columns A/B/C/D."""
    if not file_bytes or len(file_bytes) > MAX_FILE_BYTES:
        raise ResourceExcelError("请选择不超过 5 MB 的 .xlsx 文件")

    try:
        workbook = ZipFile(BytesIO(file_bytes))
    except BadZipFile:
        raise ResourceExcelError("文件不是有效的 .xlsx 工作簿") from None

    with workbook:
        names = workbook.namelist()
        sheet_paths = sorted(
            (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
            key=lambda name: int(re.search(r"sheet(\d+)\.xml", name).group(1)),
        )
        if "xl/workbook.xml" not in names or not sheet_paths:
            raise ResourceExcelError("文件缺少 Excel 工作表")
        if sum(info.file_size for info in workbook.infolist()) > MAX_UNCOMPRESSED_BYTES:
            raise ResourceExcelError("Excel 解压后内容过大")

        shared_strings = []
        if "xl/sharedStrings.xml" in names:
            try:
                strings_root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
                shared_strings = [
                    "".join(part.text or "" for part in item.findall(".//m:t", NAMESPACE))
                    for item in strings_root.findall("m:si", NAMESPACE)
                ]
            except ElementTree.ParseError:
                raise ResourceExcelError("Excel 共享文本格式无效") from None

        resources, errors = [], []
        processed_rows = 0
        for sheet_number, sheet_path in enumerate(sheet_paths, start=1):
            try:
                root = ElementTree.fromstring(workbook.read(sheet_path))
            except ElementTree.ParseError:
                raise ResourceExcelError(f"第 {sheet_number} 个工作表格式无效") from None

            columns = None
            for row in root.findall(".//m:sheetData/m:row", NAMESPACE):
                values = {
                    index: _cell_value(cell, shared_strings)
                    for cell in row.findall("m:c", NAMESPACE)
                    if (index := _column_index(cell.get("r"))) is not None
                }
                if not any(values.values()):
                    continue

                if columns is None:
                    detected = {}
                    for index, value in values.items():
                        key = _header_key(value)
                        for field, aliases in HEADER_ALIASES.items():
                            if key in aliases:
                                detected[field] = index
                    if "name" in detected and "share_link" in detected:
                        columns = detected
                        continue
                    columns = {"name": 0, "share_link": 1, "type": 2, "remarks": 3}

                row_number = row.get("r", "?")
                prefix = f"工作表 {sheet_number} 第 {row_number} 行"
                processed_rows += 1
                if processed_rows > MAX_ROWS:
                    raise ResourceExcelError(f"单次最多导入 {MAX_ROWS} 行资源")
                name = values.get(columns["name"], "").strip()
                link = values.get(columns["share_link"], "").strip()
                if not name or not link:
                    errors.append(f"{prefix}：标题和链接均不能为空")
                    continue
                if not _is_web_link(link):
                    errors.append(f"{prefix}：链接不是有效的 http(s) 地址")
                    continue

                resources.append({
                    "name": name,
                    "share_link": link,
                    "type": values.get(columns.get("type"), "").strip(),
                    "remarks": values.get(columns.get("remarks"), "").strip(),
                    "row": row_number,
                    "sheet": sheet_number,
                })
        return resources, errors
