"""Presentation-only pagination for independent tables and existing list views."""

from django import template
from django.core.paginator import Page, Paginator
from django.http import QueryDict

register = template.Library()


@register.simple_tag(takes_context=True)
def table_page(context, records, parameter="page", per_page=10):
    """Page a complete display collection without changing its source or totals."""
    if isinstance(records, Page):
        return records
    request = context.get("request")
    number = request.GET.get(parameter) if request else None
    return Paginator(records if records is not None else [], per_page).get_page(number)


@register.inclusion_tag("components/table_pagination.html", takes_context=True)
def render_table_pagination(context, page, parameter="page", label="Records"):
    """Retain filters and other tables' pages in every navigation link."""
    if not isinstance(page, Page):
        return {}
    request = context.get("request")
    query = request.GET.copy() if request else QueryDict(mutable=True)
    anchor = f"pagination-{parameter}"

    def page_url(number):
        params = query.copy()
        params[parameter] = number
        return f"?{params.urlencode()}#{anchor}"

    pages = [
        {"number": number, "url": page_url(number), "current": number == page.number}
        if isinstance(number, int) else {"gap": True}
        for number in page.paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1)
    ]
    return {
        "table_page": page,
        "pagination_label": label,
        "pagination_anchor": anchor,
        "pagination_links": pages,
        "previous_url": page_url(page.previous_page_number()) if page.has_previous() else None,
        "next_url": page_url(page.next_page_number()) if page.has_next() else None,
    }
