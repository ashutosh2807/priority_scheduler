"""Shared access and rendering checks for the three empty application pages."""
from html.parser import HTMLParser

from django.apps import apps
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse

from apps.accounts.models import AdminUser


class NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


class ApplicationPlaceholderTests(TestCase):
    applications = (
        ("sulog", "SULOG", "No SULOG sources configured yet"),
        ("eloadlog", "ELOADLOG", "No ELOADLOG sources configured yet"),
        ("mailer", "MAILER", "No mail configurations yet"),
    )

    @classmethod
    def setUpTestData(cls):
        # Non-operator accounts can browse these placeholders without invoking
        # the operational confirmation banner or the scheduler API.
        cls.user = AdminUser.objects.create_user(
            username="application-reader", employee_id="APP-READER",
            display_name="Application reader", is_active_admin=False,
        )

    def test_all_application_routes_require_login_and_preserve_destination(self):
        for name, _, _ in self.applications:
            with self.subTest(application=name):
                url = reverse(f"{name}:index")
                self.assertEqual(url, f"/{name}/")
                self.assertEqual(resolve(url).namespace, name)
                response = self.client.get(url)
                self.assertRedirects(
                    response, f"{reverse('accounts:login')}?next={url}",
                    fetch_redirect_response=False,
                )

    def test_authenticated_pages_render_their_empty_state_in_shared_layout(self):
        self.client.force_login(self.user)
        for name, title, message in self.applications:
            with self.subTest(application=name):
                response = self.client.get(reverse(f"{name}:index"))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, f"{name}/index.html")
                self.assertTemplateUsed(response, "base.html")
                self.assertContains(response, f'<h1 class="page-title h2">{title}</h1>', html=True)
                self.assertContains(response, message)

    def test_shared_desktop_and_mobile_navigation_identifies_current_application(self):
        factory = RequestFactory()
        for name, _, _ in self.applications:
            with self.subTest(application=name):
                url = reverse(f"{name}:index")
                request = factory.get(url)
                request.resolver_match = resolve(url)
                parser = NavigationParser()
                parser.feed(render_to_string("components/navigation.html", {"request": request, "user": self.user}))
                active_links = [link for link in parser.links if "active" in link.get("class", "").split()]
                self.assertEqual([link["href"] for link in active_links], [url])
                self.assertEqual(active_links[0].get("aria-current"), "page")
                for linked_name, _, _ in self.applications:
                    self.assertIn(reverse(f"{linked_name}:index"), [link["href"] for link in parser.links])

    def test_application_pages_do_not_accept_mutations(self):
        self.client.force_login(self.user)
        for name, _, _ in self.applications:
            with self.subTest(application=name):
                self.assertEqual(self.client.post(reverse(f"{name}:index"), {}).status_code, 405)

    def test_placeholder_apps_register_without_introducing_data_models(self):
        for name, title, _ in self.applications:
            with self.subTest(application=name):
                config = apps.get_app_config(name)
                self.assertEqual(config.verbose_name, title)
                self.assertEqual(list(config.get_models()), [])
