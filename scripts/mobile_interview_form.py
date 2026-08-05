from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from html import escape
from typing import Any, Callable
from zoneinfo import ZoneInfo

from flask import Flask, Response, request

from miyou_system_automation import INTERVIEW_PERSONNEL_DROPDOWNS, TABLES, TRANSFER_TO_ANCHOR_FIELD, Feishu, text_value, user_ids


SHANGHAI = ZoneInfo("Asia/Shanghai")
USED_NONCES: dict[str, float] = {}
NONCE_LOCK = threading.Lock()


def form_token_valid(provided: str) -> bool:
    expected = os.environ.get("MOBILE_FORM_TOKEN", "").strip()
    return bool(expected and provided and hmac.compare_digest(expected, provided))


def datetime_ms(value: str) -> int | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return int(parsed.timestamp() * 1000)


def invitation_day(value: str) -> str:
    if not value:
        return ""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.strftime("%Y/%m/%d")


def select_options(fs: Feishu) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for field in fs.fields(TABLES["interview"]):
        name = str(field.get("field_name") or "")
        options = [
            str(option.get("name") or "").strip()
            for option in ((field.get("property") or {}).get("options") or [])
            if str(option.get("name") or "").strip()
        ]
        if options:
            result[name] = options
    return result


def active_people_by_name(fs: Feishu) -> dict[str, list[dict[str, str]]]:
    people: dict[str, list[dict[str, str]]] = {}
    for record in fs.list_records(TABLES["personnel"], page_size=500):
        fields = record.get("fields") or {}
        if text_value(fields.get("在职状态")) != "在职" or text_value(fields.get("账号状态")) != "正常":
            continue
        name = text_value(fields.get("姓名")).strip()
        ids = user_ids(fields.get("飞书用户"))
        if name and ids:
            people[name] = [{"id": user_id} for user_id in ids]
    return people


def claim_nonce(nonce: str) -> bool:
    if not nonce:
        return False
    now = time.time()
    with NONCE_LOCK:
        for key, created_at in list(USED_NONCES.items()):
            if now - created_at > 3600:
                USED_NONCES.pop(key, None)
        if nonce in USED_NONCES:
            return False
        USED_NONCES[nonce] = now
        return True


def release_nonce(nonce: str) -> None:
    with NONCE_LOCK:
        USED_NONCES.pop(nonce, None)


def option_tags(options: list[str], selected: str = "", empty_label: str = "请选择") -> str:
    tags = [f'<option value="">{escape(empty_label)}</option>']
    for option in options:
        marker = " selected" if option == selected else ""
        tags.append(f'<option value="{escape(option)}"{marker}>{escape(option)}</option>')
    return "".join(tags)


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>{escape(title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #f5f6f8; color: #1f2329; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
    header {{ background: #fff; border-bottom: 1px solid #e5e6eb; padding: 18px 16px 14px; position: sticky; top: 0; z-index: 5; }}
    h1 {{ margin: 0; font-size: 20px; line-height: 1.4; letter-spacing: 0; }}
    main {{ width: min(100%, 720px); margin: 0 auto; padding: 14px 14px 36px; }}
    section {{ background: #fff; border-bottom: 1px solid #e5e6eb; margin-bottom: 12px; padding: 16px 14px 4px; }}
    h2 {{ margin: 0 0 14px; font-size: 16px; letter-spacing: 0; }}
    label {{ display: block; margin: 0 0 14px; font-size: 14px; font-weight: 600; }}
    input,select,textarea {{ display: block; width: 100%; margin-top: 7px; border: 1px solid #c9cdd4; border-radius: 6px; background: #fff; color: #1f2329; font: inherit; min-height: 44px; padding: 10px 11px; }}
    textarea {{ min-height: 84px; resize: vertical; }}
    .required::after {{ content: " *"; color: #d83931; }}
    .check {{ display: flex; align-items: center; gap: 9px; font-weight: 500; }}
    .check input {{ width: 20px; min-height: 20px; margin: 0; }}
    button,.button {{ width: 100%; min-height: 48px; border: 0; border-radius: 6px; background: #3370ff; color: #fff; font-size: 16px; font-weight: 700; display: flex; align-items: center; justify-content: center; text-decoration: none; }}
    .message {{ background: #fff; padding: 22px 18px; margin-top: 18px; text-align: center; }}
    .message h2 {{ font-size: 20px; }}
    .message p {{ color: #646a73; line-height: 1.7; }}
    .error {{ color: #d83931; margin: 0 0 12px; }}
  </style>
</head>
<body><header><h1>{escape(title)}</h1></header><main>{body}</main></body>
</html>"""


def render_interview_form(fs: Feishu, token: str, error: str = "") -> str:
    options = select_options(fs)
    nonce = secrets.token_urlsafe(18)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    body = f"""
<form method="post" action="/forms/interview">
  <input type="hidden" name="access_token" value="{escape(token)}">
  <input type="hidden" name="nonce" value="{escape(nonce)}">
  {error_html}
  <section>
    <h2>邀约登记</h2>
    <label class="required">候选人姓名<input name="candidate_name" required maxlength="80"></label>
    <label>联系方式<input name="contact" inputmode="tel" maxlength="50"></label>
    <label>城市/区域<input name="region" maxlength="100"></label>
    <label>年龄<input name="age" inputmode="numeric" maxlength="20"></label>
    <label>学历<select name="education">{option_tags(options.get('学历', []))}</select></label>
    <label class="required">招募人<select name="recruiter" required>{option_tags(options.get('招募人', []))}</select></label>
    <label>投递渠道<select name="channel">{option_tags(options.get('投递渠道', []))}</select></label>
    <label class="required">邀约时间<input type="datetime-local" name="invitation_time" required></label>
    <label>邀约阶段<select name="invitation_stage">{option_tags(options.get('邀约阶段', []))}</select></label>
    <label>首次联系时间<input type="datetime-local" name="first_contact_time"></label>
    <label>邀约备注<textarea name="invitation_note" maxlength="1000"></textarea></label>
  </section>
  <section>
    <h2>面试登记</h2>
    <label>面试岗位<select name="interview_position">{option_tags(options.get('面试岗位', []))}</select></label>
    <label>面试地点<select name="interview_location">{option_tags(options.get('面试地点', []))}</select></label>
    <label>面试开始时间<input type="datetime-local" name="interview_start"></label>
    <label>面试结束时间<input type="datetime-local" name="interview_end"></label>
    <label>面试官<select name="interviewer">{option_tags(options.get('面试官', []))}</select></label>
    <label>对接运营<select name="operator">{option_tags(options.get('对接运营', []))}</select></label>
    <label>面试状态<select name="interview_status">{option_tags(options.get('面试状态', []))}</select></label>
    <label>最终岗位<select name="final_position">{option_tags(options.get('最终岗位', []))}</select></label>
    <label>意向程度<select name="intention">{option_tags(options.get('意向程度', []))}</select></label>
    <label>风格/人设初判<textarea name="style_judgement" maxlength="1000"></textarea></label>
    <label>跟进情况<textarea name="follow_up" maxlength="1000"></textarea></label>
    <label>核心顾虑点<textarea name="concerns" maxlength="1000"></textarea></label>
    <label class="check"><input type="checkbox" name="create_anchor">通过转入主播</label>
  </section>
  <button type="submit">提交登记</button>
</form>"""
    return page_shell("邀约与面试登记", body)


def build_record_fields(fs: Feishu) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "WPS记录ID": f"MOBILE-{datetime.now(SHANGHAI).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}",
        "候选人姓名": request.form.get("candidate_name", "").strip(),
        "系统处理状态": "待处理",
        TRANSFER_TO_ANCHOR_FIELD: request.form.get("create_anchor") == "on",
    }
    simple_fields = {
        "联系方式": "contact",
        "城市/区域": "region",
        "年龄": "age",
        "学历": "education",
        "投递渠道": "channel",
        "邀约阶段": "invitation_stage",
        "面试地点": "interview_location",
        "面试状态": "interview_status",
        "意向程度": "intention",
        "风格/人设初判": "style_judgement",
        "跟进情况": "follow_up",
        "核心顾虑点": "concerns",
        "邀约备注": "invitation_note",
    }
    for field_name, input_name in simple_fields.items():
        value = request.form.get(input_name, "").strip()
        if value:
            fields[field_name] = value
    for field_name, input_name in {"面试岗位": "interview_position", "最终岗位": "final_position"}.items():
        value = request.form.get(input_name, "").strip()
        if value:
            fields[field_name] = [value]
    for field_name, input_name in {
        "邀约时间": "invitation_time",
        "首次联系时间": "first_contact_time",
        "面试开始时间": "interview_start",
        "面试结束时间": "interview_end",
    }.items():
        value = datetime_ms(request.form.get(input_name, "").strip())
        if value is not None:
            fields[field_name] = value
    invite_day = invitation_day(request.form.get("invitation_time", "").strip())
    if invite_day:
        fields["邀约日期（按天分组）"] = invite_day

    people = active_people_by_name(fs)
    for visible_name, input_name in {"招募人": "recruiter", "面试官": "interviewer", "对接运营": "operator"}.items():
        selected = request.form.get(input_name, "").strip()
        if not selected:
            continue
        fields[visible_name] = selected
        users = people.get(selected)
        if users:
            fields[str(INTERVIEW_PERSONNEL_DROPDOWNS[visible_name]["account_field"])] = users
    return fields


def register_mobile_interview_form(app: Flask, tenant_token: Callable[[], str]) -> None:
    @app.route("/forms/interview", methods=["GET", "POST"])
    def mobile_interview_form() -> Response:
        provided = (request.args.get("token") or request.form.get("access_token") or "").strip()
        if not form_token_valid(provided):
            return Response(page_shell("入口无效", '<div class="message"><h2>入口无效</h2><p>请从飞书“01从这里开始”重新进入。</p></div>'), status=403, mimetype="text/html")
        fs = Feishu(tenant_token())
        if request.method == "GET":
            response = Response(render_interview_form(fs, provided), mimetype="text/html")
            response.headers["Cache-Control"] = "no-store"
            return response

        candidate_name = request.form.get("candidate_name", "").strip()
        recruiter = request.form.get("recruiter", "").strip()
        invitation_time = request.form.get("invitation_time", "").strip()
        if not candidate_name or not recruiter or not invitation_time:
            return Response(render_interview_form(fs, provided, "请填写候选人姓名、招募人和邀约时间。"), status=400, mimetype="text/html")
        nonce = request.form.get("nonce", "").strip()
        if not claim_nonce(nonce):
            return Response(page_shell("请勿重复提交", '<div class="message"><h2>请勿重复提交</h2><p>这条登记已经处理，请返回飞书查看。</p></div>'), status=409, mimetype="text/html")
        try:
            fields = build_record_fields(fs)
            results = fs.batch_create(TABLES["interview"], [{"fields": fields}], batch_size=1)
            result = results[0] if results else {}
            if result.get("code") != 0:
                raise RuntimeError(str(result.get("msg") or result))
        except Exception:
            release_nonce(nonce)
            app.logger.exception("Mobile interview form submission failed")
            return Response(render_interview_form(fs, provided, "提交失败，请稍后重试。"), status=500, mimetype="text/html")

        continue_url = "/forms/interview?token=" + urllib.parse.quote(provided, safe="")
        body = f'<div class="message"><h2>登记成功</h2><p>{escape(candidate_name)} 已写入飞书面试表。</p><a class="button" href="{escape(continue_url)}">继续登记下一位</a></div>'
        return Response(page_shell("登记成功", body), mimetype="text/html")
