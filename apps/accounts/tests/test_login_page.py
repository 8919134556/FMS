import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_login_page_has_branded_glass_layout(client):
    response = client.get(reverse("accounts:login"))
    html = response.content.decode()
    assert response.status_code == 200
    assert "images/zentora-logo.png" in html
    assert 'class="auth-card"' in html
    assert "Track. Monitor. Optimize." in html
    assert 'placeholder="Email or Username"' in html and 'placeholder="Password"' in html
    assert "Forgot Password?" in html


def test_login_still_authenticates_through_the_new_form(client, admin_user):
    admin_user.set_password("Sup3r-secret-pass!")
    admin_user.save()
    response = client.post(
        reverse("accounts:login"), {"username": "admin", "password": "Sup3r-secret-pass!", "remember_me": "on"}
    )
    assert response.status_code == 302



def test_login_background_is_a_real_photo_not_an_illustration():
    from pathlib import Path

    static = Path(__file__).resolve().parents[3] / "static"
    assert (static / "images" / "login-bg.jpg").stat().st_size > 50_000
    assert not (static / "images" / "login-scene.svg").exists()
    assert "login-bg.jpg" in (static / "css" / "auth.css").read_text(encoding="utf-8")
