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
    assert row["comment"] == f"E1：\n{minus}0.1, Reason one.\n\nE2：\n-0.2，Reason two."
    assert not row["errors"]


def test_empty_rows_full_marks_and_duplicates():
    rows = calculate_rows(table("Group,E1\n,\nG,\nG,\n,Oops\n"), 0, [1], 10)
    assert len(rows) == 3
    assert rows[0]["score"] == "10"
    assert rows[0]["comment"] == ""
    assert "重复" in rows[0]["errors"][0]
    assert "缺少" in rows[2]["errors"][0]


@pytest.mark.parametrize("cell", ["-1,", "2", "-2", "1, Reason", "=1+1", "-NaN, Reason", "-1 Reason", "-1,   ", "-1.2.3, Reason", "-1 for", "-1, Reason; -0.5", "-1, ; -0.5, Reason"])
def test_bad_cell_coordinates(cell):
    row = calculate_rows(table(f'Group,E1\nG,"{cell}"\n'), 0, [1], 10)[0]
    assert row["errors"] and "B2" in row["errors"][0]
    assert row["items"][0]["raw"] == cell.strip()
    assert row["items"][0]["deduction"] is None
    assert row["comment"] == f"E1：\n{cell.strip()}"


@pytest.mark.parametrize("cell", ["No point loss, please cite your source.", "Wrong", "Use x = -2; the value is -3, not -4."])
def test_text_feedback_without_deduction(cell):
    row = calculate_rows(table(f'Group,E1\nG,"{cell}"\n'), 0, [1], 10)[0]
    assert not row["errors"]
    assert row["score"] == "10"
    assert row["comment"] == f"E1：\n{cell}"


@pytest.mark.parametrize("separator", ["; ", "；", ", ", "，", "\n"])
def test_multiple_deductions(separator):
    cell = separator.join(["-1.5 for i", "-0.5 for ii", "-0.5 for iii, Please add conclusions."])
    row = calculate_rows(table(f'Group,E1\nG,"{cell}"\n'), 0, [1], 10)[0]
    assert not row["errors"]
    assert row["score"] == "7.5"
    assert row["items"][0]["deduction"] == "2.5"
    assert row["comment"] == f"E1：\n{cell}"


def test_feedback_order_internal_newlines_and_decimal_sum():
    data = table('Group,E1,E2,E3\nG,"Advice.\nKeep this line.",,"-0.1 for i; -0.2 for ii"\n')
    row = calculate_rows(data, 0, [3, 2, 1], 10)[0]
    assert not row["errors"]
    assert row["score"] == "9.7"
    assert row["comment"] == "E1：\nAdvice.\nKeep this line.\n\nE3：\n-0.1 for i; -0.2 for ii"


def test_ambiguous_boundary_negative_is_not_silently_counted():
    row = calculate_rows(table('Group,E1\nG,"Check the result; -2 is a valid root."'), 0, [1], 10)[0]
    assert row["errors"]
    assert row["deduction"] == "0"


def test_multiple_deductions_exceed_maximum():
    row = calculate_rows(table('Group,E1\nG,"-6 for i; -5 for ii"'), 0, [1], 10)[0]
    assert "累计扣分" in row["errors"][0]


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


@pytest.mark.parametrize("raw,expected", [
    ("-2: i) (-0.5) Reason; ii) (-1.5) Other reason.", "2"),
    ("-1 for 4; -1 for 8", "2"),
    ("-0.5 (1), Reason; -0.5 for handwriting", "1.0"),
    ("-1.25, endpoint (-1,-1) is missing", "1.25"),
    ("-1 for 7: domain R\\{0,-2}; -1 for 8", "2"),
    ("-1 for iv; −2 each for missing v) and vi).", "5"),
])
def test_assignment_compatibility(raw, expected):
    from app.parsing import parse_deduction
    assert str(parse_deduction(raw)) == expected


@pytest.mark.parametrize("raw", ["not submitted", "-2, Total; -1 for i; -1 for ii"])
def test_ambiguous_feedback_requires_confirmation(raw):
    from app.parsing import parse_deduction
    with pytest.raises(InputError):
        parse_deduction(raw)
