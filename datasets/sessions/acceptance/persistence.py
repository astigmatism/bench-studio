import sys
from check import call

case = sys.argv[1]
if case == "issues-medium":
    assert call("/settings")[1]["default_assignee"] == "Jordan"
if case == "inventory-medium":
    assert call("/items")[1][0]["reorder_threshold"] == 1
if case == "issues-large":
    assert len(call("/issues/1/history")[1]) == 1
if case == "inventory-large":
    order = call("/orders")[1][0]
    assert order["status"] == "received"
    assert len(call("/orders/" + str(order["id"]) + "/history")[1]) == 1
print("Persistence passed:", case)
