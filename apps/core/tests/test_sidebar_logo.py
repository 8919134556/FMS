"""Sidebar logo: full logo when expanded, symbol only when collapsed (.fms-shell.is-collapsed)."""

from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from PIL import Image

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.core.tests.factories import role_with

pytestmark = pytest.mark.django_db
STATIC = Path(settings.BASE_DIR) / "static"


def test_sidebar_renders_both_logo_images(client):
    client.force_login(UserFactory(role=role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))))
    html = client.get(reverse("core:dashboard")).content.decode()
    assert 'class="brand-logo-full" src="/static/images/brand/zentora-logo-full.png"' in html
    assert 'class="brand-logo-mark" src="/static/images/brand/zentora-symbol.png"' in html
    assert "Zentora Info Global Solutions" in html  # accessible name (visually hidden)


def test_logo_assets_keep_their_proportions():
    full = Image.open(STATIC / "images/brand/zentora-logo-full.png")
    mark = Image.open(STATIC / "images/brand/zentora-symbol.png")
    assert full.mode == "RGBA" and round(full.width / full.height, 2) == 3.51  # transparent, uncropped artwork
    assert mark.size[0] == mark.size[1]  # square symbol


def test_collapsed_state_swaps_to_the_symbol():
    css = (STATIC / "css/components.css").read_text(encoding="utf-8")
    assert ".fms-shell.is-collapsed .brand-logo-full {" in css and ".fms-shell.is-collapsed .brand-logo-mark {" in css
