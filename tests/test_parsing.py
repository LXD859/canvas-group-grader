import io
import zipfile

import pytest
from openpyxl import Workbook

from app.parsing import InputError, calculate_rows, read_table


def table(body):
    return read_table("test.csv", body.encode("utf-8"))


@pytest.mark.parametrize("minus", ["-", "−", "–", "—", "﹣", "－"])
def test_decimal_and_reason_preserved(minus):
    data = table(f'Group,E1,E2\nG,"{minus}0.1, Reason one.","-0.2，Reason two."\n')
    row = calculate_rows(data, 0, [1, 2], "10")[0]
    assert row["score"] == "9.7"
    assert row["comment"] == f"E1: {minus}0.1, Reason one.\nE2: -0.2，Reason two."
    assert not row["errors"]


def test_empty_rows_full_marks_and_duplicates():
    rows = calculate_rows(table("Group,E1\n,\nG,\nG,\n,Oops\n"), 0, [1], 10)
    assert len(rows) == 3
    assert rows[0]["score"] == "10"
    assert rows[0]["comment"] == ""
    assert "重复" in rows[0]["errors"][0]
    assert "缺少" in rows[2]["errors"][0]


@pytest.mark.parametrize("cell", ["-1,", "Wrong", "1, Reason", "=1+1", "-NaN, Reason", "-1 Reason", "-1,   "])
def test_bad_cell_coordinates(cell):
    row = calculate_rows(table(f'Group,E1\nG,"{cell}"\n'), 0, [1], 10)[0]
    assert row["errors"] and "B2" in row["errors"][0]


def test_excessive_deduction():
    assert calculate_rows(table('Group,E1\nG,"-11, Reason"'), 0, [1], 10)[0]["errors"]


def test_gb18030_and_bom():
    for encoding in ("gb18030", "utf-8-sig"):
        data = read_table("a.csv", "小组,E1\n第一组,\n".encode(encoding))
        assert data["rows"][0][0] == "第一组"


def test_xlsx_default_sheet_bad_dimension_and_formula():
    book = Workbook()
    book.active.title = "Other"
    grading = book.create_sheet("Assignment Grading")
    grading.append(["Group", "E1"])
    grading.append(["G", "=1+1"])
    buffer = io.BytesIO()
    book.save(buffer)
    fixed = io.BytesIO()
    with zipfile.ZipFile(buffer) as source, zipfile.ZipFile(fixed, "w") as dest:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet2.xml":
                data = data.replace(b'ref="A1:B2"', b'ref="A1:A1"')
            dest.writestr(item, data)
    data = read_table("a.xlsx", fixed.getvalue())
    assert data["sheet"] == "Assignment Grading"
    assert len(data["rows"]) == 1
    assert calculate_rows(data, 0, [1], 10)[0]["errors"]


def test_invalid_mapping():
    with pytest.raises(InputError):
        calculate_rows(table("Group,E1\nG,"), 0, [0], 10)
    with pytest.raises(InputError):
        read_table("a.xls", b"data")
