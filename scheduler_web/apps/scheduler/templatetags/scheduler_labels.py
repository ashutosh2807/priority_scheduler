from django import template

register = template.Library()


@register.filter
def state_label(value):
    return str(value or "").replace("_", " ").capitalize()
