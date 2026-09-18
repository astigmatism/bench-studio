"""Private HTTP acceptance checks, executed only in a fresh verifier environment."""

import json
import sys
import urllib.request
import urllib.error
import concurrent.futures

BASE = "http://127.0.0.1:8111/api"


def call(path, data=None, method=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def check(task):
    assert call("/issues")[0] == 200 and call("/items")[0] == 200
    if task == "issues-medium":
        assert call("/settings", {"default_assignee": "  Jordan  "}, "PUT")[0] == 200
        assert call("/settings")[1]["default_assignee"] == "Jordan"
        assert call("/issues", {"title": "Inherited"})[1]["assignee"] == "Jordan"
        assert (
            call("/issues", {"title": "Explicit", "assignee": "Sam"})[1]["assignee"]
            == "Sam"
        )
        assert next(i for i in call("/issues")[1] if i["id"] == 1)["assignee"] == "Maya"
        for val in ("", "  ", "x" * 81):
            assert call("/settings", {"default_assignee": val}, "PUT")[0] == 422
    elif task == "inventory-medium":
        assert len(call("/items")[1]) >= 3
        assert call("/items")[1][0]["reorder_threshold"] == 5
        assert call("/items/1/threshold", {"reorder_threshold": 1}, "PUT")[0] == 200
        assert call("/items")[1][0]["reorder_threshold"] == 1
        for val in (-1, 2.5, True):
            assert (
                call("/items/1/threshold", {"reorder_threshold": val}, "PUT")[0] == 422
            )
        assert call("/items/9999/threshold", {"reorder_threshold": 5}, "PUT")[0] == 404
    elif task == "issues-large":
        columns = call("/columns")[1]
        assert {c["name"] for c in columns} >= {"Open", "Closed"}
        issues = call("/issues")[1]
        assert len(issues) >= 3 and all(i.get("column_id") for i in issues)
        status, column = call("/columns", {"name": "  Review  "})
        assert status == 201 and column["name"] == "Review"
        assert call("/columns", {"name": "Review"})[0] == 409
        assert call("/columns", {"name": "  "})[0] == 422
        assert call("/columns", {"name": "x" * 61})[0] == 422
        assert call("/issues/1/move", {"column_id": column["id"]}, "PUT")[0] == 200
        events = call("/issues/1/history")[1]
        assert (
            len(events) == 1
            and events[0]["to_column"] == column["id"]
            and events[0]["created_at"]
        )
        assert call("/issues/1/move", {"column_id": column["id"]}, "PUT")[0] == 200
        assert len(call("/issues/1/history")[1]) == 1
        assert call("/issues/1/move", {"column_id": 99999}, "PUT")[0] == 404
        assert call("/issues/99999/move", {"column_id": column["id"]}, "PUT")[0] == 404
        assert (
            next(i for i in call("/issues")[1] if i["id"] == 1)["column_id"]
            == column["id"]
        )
    elif task == "inventory-large":
        initial = {i["id"]: i["quantity"] for i in call("/items")[1]}
        good = {
            "supplier": "  Supply Co  ",
            "lines": [{"item_id": 1, "quantity": 7}, {"item_id": 2, "quantity": 3}],
        }
        status, order = call("/orders", good)
        assert (
            status == 201
            and order["supplier"] == "Supply Co"
            and order["status"] == "open"
        )
        for bad in (
            {"supplier": "", "lines": good["lines"]},
            {"supplier": "X", "lines": []},
            {"supplier": "X", "lines": [{"item_id": 1, "quantity": 0}]},
            {"supplier": "X", "lines": [{"item_id": 1, "quantity": 1.2}]},
            {"supplier": "X", "lines": [{"item_id": 1, "quantity": True}]},
            {"supplier": "X", "lines": [{"item_id": 1, "quantity": 1}] * 2},
        ):
            assert call("/orders", bad)[0] == 422
        assert (
            call(
                "/orders",
                {"supplier": "X", "lines": [{"item_id": 99999, "quantity": 1}]},
            )[0]
            == 404
        )
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outcomes = list(
                pool.map(
                    lambda _: call("/orders/" + str(order["id"]) + "/receive", {})[0],
                    range(2),
                )
            )
        assert sorted(outcomes) == [200, 409]
        current = {i["id"]: i["quantity"] for i in call("/items")[1]}
        assert (
            current[1] == initial[1] + 7
            and current[2] == initial[2] + 3
            and current[3] == initial[3]
        )
        history = call("/orders/" + str(order["id"]) + "/history")[1]
        assert len(history) == 1 and history[0]["created_at"]
        assert call("/orders/99999/receive", {})[0] == 404
    print("HTTP acceptance passed:", task)


if __name__ == "__main__":
    check(sys.argv[1])
