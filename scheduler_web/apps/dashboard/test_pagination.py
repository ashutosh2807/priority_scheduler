from html import unescape
from urllib.parse import parse_qs, urlsplit

from django.core.paginator import Paginator
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase

from .templatetags.table_pagination import render_table_pagination, table_page


class TablePaginationTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_independent_pages_preserve_all_filters_and_repeated_values(self):
        request = self.factory.get(
            "/calendar/?date=2026-09-11&q=Cash+%26+Deposits&status=READY"
            "&tag=a&tag=b&tasks_page=2&plan_page=3"
        )
        context = {"request": request}
        records = list(range(36))
        tasks = table_page(context, records, "tasks_page", 10)
        plan = table_page(context, records, "plan_page", 5)
        self.assertEqual(list(tasks), list(range(10, 20)))
        self.assertEqual(list(plan), list(range(10, 15)))
        self.assertEqual(len(records), 36)
        footer = render_table_pagination(context, tasks, "tasks_page", "Tasks")
        link = urlsplit(footer["next_url"])
        self.assertEqual(link.fragment, "pagination-tasks_page")
        self.assertEqual(parse_qs(link.query), {
            "date": ["2026-09-11"], "q": ["Cash & Deposits"], "status": ["READY"],
            "tag": ["a", "b"], "tasks_page": ["3"], "plan_page": ["3"],
        })

    def test_existing_server_page_is_not_paginated_again(self):
        page = Paginator(list(range(71)), 25).page(2)
        request = self.factory.get("/accounts/?page=2")
        self.assertIs(table_page({"request": request}, page, "page", 10), page)
        html = render_to_string("components/table_pagination.html",
            render_table_pagination({"request": request}, page, "page", "Administrators"))
        self.assertIn("26–50", html)
        self.assertIn("71", html)
        self.assertIn('aria-current="page"', html)

    def test_empty_single_invalid_and_out_of_range_pages(self):
        for value, expected in [("bad", 1), ("0", 3), ("999", 3)]:
            request = self.factory.get("/", {"tasks_page": value})
            page = table_page({"request": request}, list(range(21)), "tasks_page", 10)
            self.assertEqual(page.number, expected)
        for records, expected in [([], "0 records"), ([1], "1–1")]:
            page = table_page({}, records)
            html = render_to_string("components/table_pagination.html",
                render_table_pagination({}, page))
            self.assertIn(expected, html)
            self.assertNotIn("<nav", html)

    def test_long_page_range_has_bounded_links(self):
        page = Paginator(list(range(2000)), 10).page(101)
        footer = render_table_pagination({}, page)
        self.assertLessEqual(len(footer["pagination_links"]), 7)
        self.assertTrue(any(item.get("gap") for item in footer["pagination_links"]))

    def test_day_table_page_two_retains_confirmation_identity(self):
        rows = [{"id": index, "name": f"Task {index:02}", "report_date": "2026-09-10",
                 "occurrence_key": f"occurrence-{index}", "confirmation_needed": True,
                 "can_confirm": True, "confirmed": False} for index in range(1, 13)]
        request = self.factory.get("/scheduler/day/?tasks_page=2&mine=1&date=2026-09-11")
        html = render_to_string("scheduler/_day_table.html", {
            "rows": rows, "request": request, "csrf_token": "test-token",
        })
        self.assertNotIn("Task 01", html)
        self.assertIn("Task 11", html)
        self.assertIn("Task 12", html)
        self.assertEqual(html.count('name="occurrence_key"'), 2)
        self.assertIn('value="occurrence-11"', html)
        self.assertIn('value="occurrence-12"', html)
        self.assertIn("11–12", html)
        self.assertIn("mine=1&date=2026-09-11", unescape(html))

    def test_two_rendered_pagers_have_distinct_destinations(self):
        template = Template("""{% load table_pagination %}
            {% table_page records 'assignments_page' 10 as assignments %}
            {% table_page records 'delegations_page' 10 as delegations %}
            {% render_table_pagination assignments 'assignments_page' 'Assignments' %}
            {% render_table_pagination delegations 'delegations_page' 'Delegations' %}
        """)
        html = template.render(Context({"records": list(range(25))}))
        self.assertEqual(html.count('id="pagination-assignments_page"'), 1)
        self.assertEqual(html.count('id="pagination-delegations_page"'), 1)
        self.assertIn('aria-label="Assignments pagination"', html)
        self.assertIn('aria-label="Delegations pagination"', html)
