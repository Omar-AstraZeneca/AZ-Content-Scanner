from app.scanner.session_state import platform_covered, platform_details


def cookie(name, domain, value="1", path="/"):
    return {"name": name, "domain": domain, "path": path, "value": value}


def test_azmed_accepts_portal_and_identity_session_cookies():
    assert platform_covered([cookie("JSESSIONID", "www.azmed.cr")], "azmed")
    assert platform_covered([cookie("SESSION", "identity.astrazeneca.com", path="/csdcidentity/")], "azmed")


def test_azmed_ignores_balancer_and_analytics_cookies():
    cookies = [
        cookie("AWSALB", "www.azmed.cr"),
        cookie("AWSALBCORS", "www.azmed.cr"),
        cookie("_ga", ".azmed.cr"),
    ]
    assert not platform_covered(cookies, "azmed")


def test_veeva_login_jsessionid_alone_is_not_a_session():
    assert not platform_covered([cookie("JSESSIONID", "login.veevavault.com", path="/auth")], "veeva")


def test_veeva_accepts_post_login_cookies():
    assert platform_covered([cookie("userProfile", ".login.veevavault.com")], "veeva")
    assert platform_covered(
        [cookie("TK", ".astrazeneca-promomats.veevavault.com")], "veeva"
    )
    assert platform_covered(
        [cookie("lastActiveTS", "astrazeneca-promomats.veevavault.com", path="/ui")], "veeva"
    )


def test_kaltura_cloudflare_cookie_is_not_a_session():
    assert not platform_covered([cookie("__cf_bm", ".kmc.kaltura.com")], "kaltura")
    assert platform_covered([cookie("ks", ".kmc.kaltura.com")], "kaltura")


def test_empty_cookie_value_never_counts():
    assert not platform_covered([cookie("JSESSIONID", "www.azmed.cr", value="")], "azmed")


def test_platform_details_counts_only_session_cookies():
    cookies = [
        cookie("JSESSIONID", "www.azmed.cr"),
        cookie("AWSALB", "www.azmed.cr"),
        cookie("JSESSIONID", "login.veevavault.com", path="/auth"),
        cookie("userProfile", ".login.veevavault.com"),
        cookie("__cf_bm", ".kmc.kaltura.com"),
    ]
    details = platform_details(cookies)
    assert details["azmed"]["covered"] is True
    assert details["azmed"]["cookies"] == 1
    assert details["veeva"]["covered"] is True
    assert details["veeva"]["cookies"] == 1
    assert details["kaltura"]["covered"] is False
    assert details["kaltura"]["cookies"] == 0
