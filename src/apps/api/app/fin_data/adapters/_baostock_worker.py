"""Short-lived BaoStock worker: one JSON request on stdin, one JSON result on stdout, then exit.

Run as ``python -I _baostock_worker.py``. It exists so that pandas (imported by baostock) is released when the
process exits and so that baostock's non-thread-safe global login never lives in the API process. BaoStock prints
progress text on stdout; that is redirected to stderr so stdout carries only the final marker line.
"""
import contextlib
import json
import sys

MARKER = "@@BAOSTOCK_RESULT@@"
KDATA_FIELDS = "date,code,open,high,low,close,volume,amount,tradestatus"
FINANCIAL_QUERIES = {"profit": "query_profit_data", "balance": "query_balance_data", "cash_flow": "query_cash_flow_data"}


def _rows(rs):
    if rs.error_code != "0":
        raise RuntimeError(f"baostock error {rs.error_code}: {rs.error_msg}")
    out = []
    while rs.next():
        out.append(rs.get_row_data())
    return {"fields": list(rs.fields), "rows": out}


def _run(bs, request):
    op = request.get("op")
    if op == "stock_basic":
        return _rows(bs.query_stock_basic())
    if op == "k_data":
        result = {}
        for flag in request.get("adjustflags", ["3"]):
            result[str(flag)] = _rows(bs.query_history_k_data_plus(
                request["code"], KDATA_FIELDS, start_date=request.get("start"), end_date=request.get("end"),
                frequency="d", adjustflag=str(flag)))
        return result
    if op == "financials":
        result = {}
        for kind in request.get("kinds", []):
            fn = getattr(bs, FINANCIAL_QUERIES[kind])
            result[kind] = [dict(year=y, quarter=q, **_rows(fn(code=request["code"], year=y, quarter=q)))
                            for y, q in request.get("periods", [])]
        return result
    raise RuntimeError(f"unknown op {op!r}")


def main():
    request = json.loads(sys.stdin.read())
    try:
        with contextlib.redirect_stdout(sys.stderr):
            import baostock as bs
            login = bs.login()
            if login.error_code != "0":
                raise RuntimeError(f"login failed: {login.error_msg}")
            try:
                result = _run(bs, request)
            finally:
                bs.logout()
        payload = {"ok": True, "result": result}
    except Exception as error:  # reported to the parent as a failure, never a traceback with request data
        payload = {"ok": False, "error": f"{type(error).__name__}: {str(error)[:200]}"}
    sys.stdout.write(MARKER + json.dumps(payload) + "\n")


if __name__ == "__main__":
    main()
