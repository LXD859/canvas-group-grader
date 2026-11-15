"""Read untrusted spreadsheets without executing formulas or changing originals."""
import csv
import io
import re
import zipfile
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLS = 200
DEDUCTION_START = re.compile(r"(?:^|(?<=[;；,，\n\r]))[^\S\r\n]*[-−–—﹣－]\s*(?=[\d.]|NaN|Inf)", re.IGNORECASE)
DEDUCTION = re.compile(r"(\d+(?:\.\d+)?|\.\d+)(?=\s|[,，]|$)")
REASON = re.compile(r"(?:\s*[,，]\s*|\s+for\s+)(.*)", re.IGNORECASE | re.DOTALL)
BARE_NUMBER = re.compile(r"[+＋−–—﹣－-]?\s*(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


class InputError(ValueError):
    pass


def text(value):
    return "" if value is None else str(value).strip()


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InputError("满分必须是有效数字。") from None
    if not result.is_finite() or result < 0:
        raise InputError("满分必须是非负有限数字。")
    return result


def read_table(filename, content, sheet=None, encoding="auto"):
    if len(content) > MAX_BYTES:
        raise InputError("文件超过 10 MB。")
    if filename.lower().endswith(".csv"):
        if encoding not in {"auto", "utf-8-sig", "gb18030"}:
            raise InputError("不支持该编码。")
        decoded = None
        for enc in (["utf-8-sig", "gb18030"] if encoding == "auto" else [encoding]):
            try:
                decoded = content.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise InputError("无法解码 CSV，请选择 UTF-8 或 GB18030。")
        try:
            rows = []
            for row in csv.reader(io.StringIO(decoded), strict=True):
                rows.append(row)
                if len(rows) > MAX_ROWS + 1 or len(row) > MAX_COLS:
                    raise InputError("文件最多支持 5000 行数据和 200 列。")
        except csv.Error:
            raise InputError("CSV 格式有误，请检查引号和分隔符。") from None
        sheets, selected = ["CSV"], "CSV"
    elif filename.lower().endswith(".xlsx"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 50 * 1024 * 1024:
                    raise InputError("Excel 解压后超过 50 MB。")
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
            try:
                sheets = book.sheetnames
                selected = sheet or ("Assignment Grading" if "Assignment Grading" in sheets else sheets[0])
                if selected not in sheets:
                    raise InputError("工作表不存在。")
                tab = book[selected]
                # Some exporters declare A1:A1 even when the sheet contains many rows.
                tab.reset_dimensions()
                rows = []
                for row in tab.iter_rows(values_only=True):
                    rows.append(list(row))
                    if len(rows) > MAX_ROWS + 1 or len(row) > MAX_COLS:
                        raise InputError("文件最多支持 5000 行数据和 200 列。")
            finally:
                book.close()
        except InputError:
            raise
        except Exception:
            raise InputError("无法读取 Excel，请使用未加密的 .xlsx 文件。") from None
    else:
        raise InputError("仅支持 .xlsx 和 .csv 文件。")
    if not rows or not any(text(x) for x in rows[0]):
        raise InputError("第一行必须包含列标题。")
    width = max(map(len, rows))
    rows = [[text(x) for x in r] + [""] * (width - len(r)) for r in rows]
    headers = rows[0]
    return {"sheets": sheets, "sheet": selected, "headers": headers, "rows": rows[1:]}


def parse_deduction(raw):
    if re.fullmatch(r"(?:not submitted|未提交)[.!。]?", raw, re.IGNORECASE):
        raise InputError("未提交但没有注明扣分，请填写明确扣分金额。")
    raw = re.sub(r"^([-−–—﹣－]\s*\d+(?:\.\d+)?)[：:]", r"\1,", raw)
    raw = re.sub(r"([-−–—﹣－]\s*\d+(?:\.\d+)?)\s+\((\d+)\)\s*[,，]", r"\1 for part \2,", raw)
    raw = re.sub(r"\bfor\s+(\d+)", r"for part \1", raw)
    def expand_each(match):
        amount, first, second = match.groups()
        return f"-{amount} for missing {first}); -{amount} for missing {second})"
    raw = re.sub(r"[-−–—﹣－](\d+(?:\.\d+)?)\s+each for missing\s+([ivx]+)\)\s+and\s+([ivx]+)\)", expand_each, raw, flags=re.IGNORECASE)
    if raw.startswith("="):
        raise InputError("不能使用公式，请填写评论原文。")
    if BARE_NUMBER.fullmatch(raw):
        raise InputError("单独数字的含义待确认，请补充扣分说明或改为文字评论。")
    starts = [match for match in DEDUCTION_START.finditer(raw)
              if match.start() == 0 or not re.match(r"\d+(?:\.\d+)?\s*[)}\]]", raw[match.end():])]
    if len(starts) > 1 and re.search(r"\b(total|overall deduction)\b|总扣分|合计扣", raw, re.IGNORECASE):
        raise InputError("总扣分与多笔负数分项同时出现，请保留总扣分并将分项金额写为正数。")
    if not starts:
        if re.match(r"^[+＋]?\s*(?:\d|\.\d)", raw):
            raise InputError("以数字开头的内容含义待确认；扣分请使用 -1, 原因 或 -1 for ii...。")
        return Decimal(0)
    total = Decimal(0)
    for position, start in enumerate(starts):
        end = starts[position + 1].start() if position + 1 < len(starts) else len(raw)
        entry = raw[start.end():end].rstrip(" \t\r\n;；,，")
        amount = DEDUCTION.match(entry)
        if not amount or len(amount.group(1)) > 24:
            raise InputError("扣分数字无法识别，请使用 -1.5, 原因 或 -1.5 for ii...。")
        reason = REASON.fullmatch(entry[amount.end():])
        if not reason or not any(character.isalpha() for character in reason.group(1)):
            raise InputError("扣分条目缺少明确说明，请使用 -1.5, 原因 或 -1.5 for ii...。")
        total += Decimal(amount.group(1))
    return total


def calculate_rows(table, group_column, question_columns, maximum):
    maximum = number(maximum)
    width = len(table["headers"])
    if not question_columns or len(set(question_columns)) != len(question_columns):
        raise InputError("请选择至少一个不重复的题目列。")
    indices = [group_column, *question_columns]
    if any(i < 0 or i >= width for i in indices) or group_column in question_columns:
        raise InputError("小组列和题目列必须有效且互不重叠。")
    question_columns = sorted(question_columns)
    labels = [table["headers"][i] for i in question_columns]
    if any(not label for label in labels) or len(set(labels)) != len(labels):
        raise InputError("所选题目列必须有唯一的题号标题。")
    names = Counter(r[group_column] for r in table["rows"] if r[group_column])
    results = []
    for row_number, row in enumerate(table["rows"], 2):
        if not any(row):
            continue
        name, errors, items, comments = row[group_column], [], [], []
        if not name:
            errors.append(f"{get_column_letter(group_column + 1)}{row_number}：缺少小组名称。")
        elif names[name] > 1:
            errors.append("表格中存在重复小组名称。")
        if name.startswith("="):
            errors.append("小组名称必须为文本，不能使用公式。")
        with localcontext() as context:
            context.prec = 64
            total = Decimal(0)
            for index in question_columns:
                raw, label = row[index], table["headers"][index]
                coordinate = f"{get_column_letter(index + 1)}{row_number}"
                if not raw:
                    items.append({"question": label, "deduction": "0", "raw": "", "cell": coordinate})
                    continue
                comments.append(f"{label}：\n{raw}")
                try:
                    deduction = parse_deduction(raw)
                except InputError as exc:
                    errors.append(f"{coordinate}（{label}）：{exc}")
                    items.append({"question": label, "deduction": None, "raw": raw, "cell": coordinate})
                    continue
                total += deduction
                items.append({"question": label, "deduction": str(deduction), "raw": raw, "cell": coordinate})
            score = maximum - total
        if score < 0:
            errors.append("累计扣分超过作业满分。")
        comment = "\n\n".join(comments)
        if len(comment) > 16000:
            errors.append("合并评语超过 16000 字符。")
        results.append({"row": row_number, "name": name, "items": items,
                        "deduction": str(total), "score": str(score), "comment": comment,
                        "errors": errors})
    if not results:
        raise InputError("工作表中没有小组数据。")
    return results
