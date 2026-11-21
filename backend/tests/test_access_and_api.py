import json

from conftest import api_event

from ocrsvc import db, settings, users
from ocrsvc.handlers import api, tesseract, triggers


def call(method, path, sub=None, email="", body=None):
    resp = api.handler(api_event(method, path, sub, email, body), None)
    return resp["statusCode"], json.loads(resp["body"])


def signup(email):
    return triggers.pre_sign_up({"request": {"userAttributes": {"email": email}}}, None)


def test_invite_mode_blocks_strangers(aws):
    settings.save({"accessMode": "invite"})
    try:
        signup("stranger@example.com")
        raise AssertionError("should have been rejected")
    except Exception as exc:  # noqa: BLE001
        assert "invite only" in str(exc)
    signup("boss@example.com")  # superadmin always allowed
    db.put({**db.invite_key("friend@example.com"), "email": "friend@example.com", "credits": 7})
    signup("friend@example.com")
    triggers.post_confirmation({"triggerSource": "PostConfirmation_ConfirmSignUp",
                                "request": {"userAttributes": {"sub": "f1", "email": "friend@example.com"}}}, None)
    p = users.get_profile("f1")
    assert p["status"] == "active" and p["credits"] == 7


def test_approval_mode_and_admin_flow(aws):
    settings.save({"accessMode": "approval"})
    users.create_profile("admin", "boss@example.com")
    users.create_profile("u1", "new@example.com")
    assert users.get_profile("admin")["role"] == "superadmin"
    assert users.get_profile("u1")["status"] == "pending"

    code, body = call("GET", "/api/jobs", "u1")
    assert code == 403 and body["code"] == "account_pending"
    code, body = call("GET", "/api/me", "u1")
    assert code == 200 and body["user"]["status"] == "pending"

    code, _ = call("GET", "/api/admin/users", "u1")
    assert code == 403
    code, body = call("GET", "/api/admin/users", "admin")
    assert code == 200 and len(body["users"]) == 2
    code, body = call("PATCH", "/api/admin/users/u1", "admin", body={"status": "active", "creditsDelta": 25})
    assert code == 200 and body["user"]["status"] == "active" and body["user"]["credits"] == 75
    code, _ = call("GET", "/api/jobs", "u1")
    assert code == 200


def test_settings_validation(aws):
    users.create_profile("admin", "boss@example.com")
    code, body = call("PUT", "/api/admin/settings", "admin", body={"accessMode": "everyone"})
    assert code == 400
    code, body = call("PUT", "/api/admin/settings", "admin",
                      body={"accessMode": "open", "billingEnabled": True, "prices": {"textract": 3, "tesseract": 1}})
    assert code == 200 and body["settings"]["prices"]["textract"] == 3
    code, body = call("GET", "/api/public/config")
    assert code == 200 and body["accessMode"] == "open" and body["billingEnabled"] is True


def test_no_credits_blocks_new_jobs(aws):
    settings.save({"accessMode": "open", "billingEnabled": True, "freeCreditsOnSignup": 0})
    users.create_profile("u1", "a@example.com")
    code, body = call("POST", "/api/jobs", "u1", body={"engine": "textract"})
    assert code == 402


def test_unauthenticated_and_unknown(aws):
    assert call("GET", "/api/jobs")[0] == 401
    assert call("GET", "/api/nope", "x")[0] == 404


def test_parse_tesseract_tsv(tmp_path):
    tsv = tmp_path / "out.tsv"
    rows = [
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "1\t1\t0\t0\t0\t0\t0\t0\t1000\t2000\t-1\t",
        "5\t1\t1\t1\t1\t1\t100\t200\t50\t20\t95.5\tHello",
        "5\t1\t1\t1\t1\t2\t160\t205\t60\t20\t90.5\tworld",
        "1\t2\t0\t0\t0\t0\t0\t0\t1000\t2000\t-1\t",
        "5\t2\t1\t1\t1\t1\t10\t10\t10\t10\t80\tPage2",
    ]
    tsv.write_text("\n".join(rows) + "\n")
    pages = tesseract.parse_tsv(str(tsv), first_page=11)
    assert [p["page"] for p in pages] == [11, 12]
    line = pages[0]["lines"][0]
    assert line["text"] == "Hello world" and line["conf"] == 93.0
    assert line["bbox"] == [0.1, 0.1, 0.12, 0.0125]
