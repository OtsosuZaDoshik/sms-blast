#!/usr/bin/env python3
"""Веб-панель рассылки SMS через Android-телефон."""

import csv
import io
import os
import time

from flask import (Flask, Response, flash, jsonify, redirect, render_template,
                   request, url_for)
from werkzeug.utils import secure_filename

from smsblast import (config, db, discovery, gateway as gw, importer, phones,
                      templating, usb)
from smsblast.sender import sender

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 МБ на файл базы

os.makedirs(config.UPLOAD_DIR, exist_ok=True)
os.makedirs(config.EXPORT_DIR, exist_ok=True)

db.init_db()
sender.ensure_running()


@app.template_filter("segments")
def segments_filter(text):
    encoding, length, count = phones.segments(text or "")
    return "{} симв. · {} SMS · {}".format(length, count, encoding)


# --- дашборд -----------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        totals=db.dashboard_totals(),
        campaigns=db.list_campaigns(),
        state=sender.state,
    )


# --- контакты ----------------------------------------------------------------

@app.route("/contacts")
def contacts():
    search = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    total = db.count_contacts(search)
    rows = db.list_contacts(search, limit=per_page, offset=(page - 1) * per_page)
    return render_template(
        "contacts.html",
        contacts=rows,
        total=total,
        total_all=db.count_contacts(),
        page=page,
        pages=max(1, -(-total // per_page)),
        search=search,
    )


@app.route("/contacts/add", methods=["POST"])
def contacts_add():
    settings = db.get_settings()
    phone, error = phones.normalize(
        request.form.get("phone"), settings["default_country_code"]
    )
    if error:
        flash("Номер не принят: {}".format(error), "error")
    else:
        action = db.upsert_contact(
            phone,
            request.form.get("name", "").strip(),
            consent=1 if request.form.get("consent") else 0,
            source="вручную",
        )
        flash("Контакт {}: {}".format(phone, "добавлен" if action == "added" else "обновлён"),
              "ok")
    return redirect(url_for("contacts"))


@app.route("/contacts/<int:contact_id>/delete", methods=["POST"])
def contacts_delete(contact_id):
    db.delete_contact(contact_id)
    flash("Контакт удалён", "ok")
    return redirect(request.referrer or url_for("contacts"))


@app.route("/contacts/delete-all", methods=["POST"])
def contacts_delete_all():
    """Полная очистка базы контактов — с подтверждением и резервной копией."""
    if request.form.get("confirm", "").strip().upper() != "УДАЛИТЬ":
        flash("Удаление отменено: подтверждение не совпало", "error")
        return redirect(url_for("contacts"))

    rows = db.list_contacts(limit=1000000)
    if not rows:
        flash("Контактов и так нет", "warn")
        return redirect(url_for("contacts"))

    # Операция необратима, поэтому сначала выгружаем базу на диск.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(config.EXPORT_DIR, "contacts-backup-{}.csv".format(stamp))
    # Имя со временем до секунды: два удаления подряд не должны молча затирать
    # предыдущую копию — иначе она перестаёт быть страховкой.
    attempt = 2
    while os.path.exists(backup):
        backup = os.path.join(
            config.EXPORT_DIR, "contacts-backup-{}-{}.csv".format(stamp, attempt)
        )
        attempt += 1
    with open(backup, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["телефон", "имя", "согласие", "источник", "поля"])
        for row in rows:
            writer.writerow([row["phone"], row["name"], row["consent"],
                             row["source"], row["fields_json"]])

    removed = db.delete_all_contacts()
    flash("Удалено контактов: {}. Резервная копия: {}".format(removed, backup), "ok")
    flash("Стоп-лист и отчёты по кампаниям сохранены", "warn")
    return redirect(url_for("contacts"))


@app.route("/contacts/import", methods=["GET", "POST"])
def contacts_import():
    if request.method == "GET":
        return render_template("import.html")

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("Файл не выбран", "error")
        return redirect(url_for("contacts_import"))

    name = secure_filename(uploaded.filename) or "base.csv"
    path = os.path.join(config.UPLOAD_DIR, "{}-{}".format(int(time.time()), name))
    uploaded.save(path)

    try:
        headers, rows = importer.read_table(path)
    except Exception as exc:
        flash("Не удалось прочитать файл: {}".format(exc), "error")
        return redirect(url_for("contacts_import"))

    if not rows:
        flash("В файле нет строк с данными", "error")
        return redirect(url_for("contacts_import"))

    return render_template(
        "import_map.html",
        path=os.path.basename(path),
        headers=headers,
        rows=rows[:8],
        row_count=len(rows),
        phone_column=importer.guess_phone_column(headers, rows),
        name_column=importer.guess_column(headers, importer.NAME_HEADERS),
        consent_column=importer.guess_column(headers, importer.CONSENT_HEADERS),
    )


@app.route("/contacts/import/confirm", methods=["POST"])
def contacts_import_confirm():
    path = os.path.join(config.UPLOAD_DIR, secure_filename(request.form["path"]))
    if not os.path.exists(path):
        flash("Загруженный файл не найден, повторите загрузку", "error")
        return redirect(url_for("contacts_import"))

    headers, rows = importer.read_table(path)
    settings = db.get_settings()
    result = importer.import_rows(
        rows,
        phone_column=request.form["phone_column"],
        name_column=request.form.get("name_column") or None,
        consent_column=request.form.get("consent_column") or None,
        default_consent=bool(request.form.get("default_consent")),
        country_code=settings["default_country_code"],
        source=os.path.basename(path),
    )

    flash(
        "Импорт: добавлено {added}, обновлено {updated}, дублей {duplicates}, "
        "в стоп-листе {optout}, отбраковано {invalid}".format(**result),
        "ok",
    )
    for line in result["errors"][:10]:
        flash(line, "warn")
    return redirect(url_for("contacts"))


# --- стоп-лист ---------------------------------------------------------------

@app.route("/optout", methods=["GET", "POST"])
def optout():
    if request.method == "POST":
        settings = db.get_settings()
        added = 0
        for raw in request.form.get("phones", "").replace(",", "\n").splitlines():
            if not raw.strip():
                continue
            phone, error = phones.normalize(raw, settings["default_country_code"])
            if phone:
                db.add_optout(phone, request.form.get("reason", "добавлен вручную"))
                added += 1
        flash("В стоп-лист добавлено номеров: {}".format(added), "ok")
        return redirect(url_for("optout"))

    return render_template("optout.html", rows=db.list_optout())


@app.route("/optout/remove", methods=["POST"])
def optout_remove():
    db.remove_optout(request.form["phone"])
    flash("Номер убран из стоп-листа", "ok")
    return redirect(url_for("optout"))


# --- кампании ----------------------------------------------------------------

@app.route("/campaigns/new", methods=["GET", "POST"])
def campaign_new():
    settings = db.get_settings()

    if request.method == "GET":
        return render_template(
            "campaign_new.html",
            settings=settings,
            fields=db.known_field_names(),
            contacts_all=db.count_audience(),
            contacts_consent=db.count_audience(only_consent=True),
        )

    name = request.form.get("name", "").strip() or "Без названия"
    template = request.form.get("template", "").strip()
    if not template:
        flash("Пустой текст сообщения", "error")
        return redirect(url_for("campaign_new"))

    only_consent = bool(request.form.get("only_consent"))
    audience = db.list_audience(only_consent=only_consent)

    queue, skipped = [], 0
    for row in audience:
        text = templating.render(template, db.contact_fields(row))
        if not text:
            skipped += 1
            continue
        queue.append((row["id"], row["phone"], text))

    if not queue:
        flash("Некому отправлять: аудитория пуста после фильтров", "error")
        return redirect(url_for("campaign_new"))

    campaign_id = db.create_campaign(
        name=name,
        template=template,
        delay_sec=float(request.form.get("delay_sec", settings["delay_sec"])),
        jitter_sec=float(request.form.get("jitter_sec", settings["jitter_sec"])),
        hourly_limit=int(request.form.get("hourly_limit", settings["hourly_limit"])),
        sim_number=int(request.form.get("sim_number", settings["sim_number"])),
    )
    db.queue_messages(campaign_id, queue)

    flash(
        "Кампания создана: {} получателей, пропущено {}".format(len(queue), skipped),
        "ok",
    )
    return redirect(url_for("campaign", campaign_id=campaign_id))


@app.route("/campaigns/preview", methods=["POST"])
def campaign_preview():
    """Живой предпросмотр на первых контактах — вызывается из формы через fetch."""
    template = request.json.get("template", "")
    only_consent = bool(request.json.get("only_consent"))
    rows = db.list_audience(only_consent=only_consent, limit=3)

    samples = []
    for row in rows:
        fields = db.contact_fields(row)
        text = templating.render(template, fields)
        encoding, length, count = phones.segments(text)
        samples.append({
            "phone": row["phone"],
            "text": text,
            "info": "{} симв. · {} SMS · {}".format(length, count, encoding),
            "missing": templating.missing_for(template, fields),
        })

    total = db.count_audience(only_consent=only_consent)
    encoding, length, count = phones.segments(template)
    return jsonify({
        "samples": samples,
        "total": total,
        "template_info": "{} симв. · ~{} SMS на получателя · {}".format(
            length, count, encoding
        ),
        "total_segments": count * total,
    })


@app.route("/campaigns/<int:campaign_id>")
def campaign(campaign_id):
    row = db.get_campaign(campaign_id)
    if row is None:
        flash("Кампания не найдена", "error")
        return redirect(url_for("index"))
    return render_template(
        "campaign.html",
        campaign=row,
        stats=db.campaign_stats(campaign_id),
        messages=db.list_messages(campaign_id, limit=200),
    )


@app.route("/campaigns/<int:campaign_id>/<action>", methods=["POST"])
def campaign_action(campaign_id, action):
    if action == "start":
        running = db.running_campaign_id()
        if running and running != campaign_id:
            flash("Уже идёт кампания №{} — сначала остановите её".format(running), "error")
        else:
            settings = db.get_settings()
            # По кабелю сначала поднимаем проброс: он не переживает
            # перевтыкание кабеля и перезагрузку телефона.
            if settings.get("connection_mode") == "usb":
                try:
                    usb.ensure_forward(
                        int(settings.get("usb_local_port", 18080) or 18080),
                        int(settings.get("gateway_port", 8080) or 8080),
                    )
                except usb.UsbError as exc:
                    flash("USB: {}".format(exc.full()), "error")
                    return redirect(url_for("campaign", campaign_id=campaign_id))

            ok, note = gw.from_settings(settings).health()
            if not ok:
                flash("Шлюз недоступен: {}".format(note), "error")
            else:
                sender.start_campaign(campaign_id)
                flash("Рассылка запущена", "ok")
    elif action == "pause":
        sender.pause_campaign(campaign_id, "Пауза вручную")
        flash("Пауза", "ok")
    elif action == "stop":
        sender.stop_campaign(campaign_id)
        flash("Рассылка остановлена", "ok")
    elif action == "requeue-failed":
        with db.tx() as conn:
            conn.execute(
                "UPDATE messages SET status = 'queued', attempts = 0, error = NULL "
                "WHERE campaign_id = ? AND status = 'failed'",
                (campaign_id,),
            )
        flash("Неудачные сообщения возвращены в очередь", "ok")
    return redirect(url_for("campaign", campaign_id=campaign_id))


@app.route("/campaigns/<int:campaign_id>/progress.json")
def campaign_progress(campaign_id):
    row = db.get_campaign(campaign_id)
    stats = db.campaign_stats(campaign_id)
    recent = db.list_messages(campaign_id, limit=25)
    return jsonify({
        "status": row["status"] if row else "unknown",
        "stats": stats,
        "state": sender.state,
        "recent": [
            {
                "phone": m["phone"],
                "status": m["status"],
                "error": m["error"] or "",
                "sent_at": m["sent_at"] or "",
            }
            for m in recent
        ],
    })


@app.route("/campaigns/<int:campaign_id>/export.csv")
def campaign_export(campaign_id):
    rows = db.list_messages(campaign_id, limit=1000000)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["телефон", "статус", "попыток", "отправлено", "ошибка", "текст"])
    for m in rows:
        writer.writerow([m["phone"], m["status"], m["attempts"],
                         m["sent_at"] or "", m["error"] or "", m["text"]])
    # BOM, чтобы Excel на macOS не сломал кириллицу.
    payload = "﻿" + buffer.getvalue()
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename=campaign-{}.csv".format(campaign_id)},
    )


# --- настройки ---------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        keys = ["connection_mode", "usb_local_port",
                "gateway_host", "gateway_port", "gateway_user", "gateway_password",
                "default_country_code", "delay_sec", "jitter_sec", "hourly_limit",
                "sim_number", "max_attempts"]
        values = {k: request.form.get(k, "") for k in keys if k in request.form}
        # Пустое поле пароля означает «оставить как было».
        if not values.get("gateway_password"):
            values.pop("gateway_password", None)
        db.save_settings(values)
        flash("Настройки сохранены", "ok")
        return redirect(url_for("settings_page"))

    settings = db.get_settings()
    return render_template("settings.html", settings=settings)


@app.route("/settings/health", methods=["POST"])
def settings_health():
    ok, note = gw.from_settings(db.get_settings()).health()
    return jsonify({"ok": ok, "note": note})


@app.route("/settings/discover", methods=["POST"])
def settings_discover():
    """Ищет шлюз в локальной сети. Пароль при сканировании не отправляется."""
    settings = db.get_settings()
    port = int(settings.get("gateway_port", 8080) or 8080)
    candidates, scanned = discovery.scan(port=port)
    return jsonify({
        "port": port,
        "scanned": scanned,
        "candidates": candidates,
        "current": settings.get("gateway_host", ""),
    })


@app.route("/settings/use-host", methods=["POST"])
def settings_use_host():
    """Сохраняет найденный адрес и сразу проверяет его с логином и паролем."""
    payload = request.get_json(silent=True) or {}
    host = str(payload.get("host", "")).strip()
    port = str(payload.get("port", "")).strip()
    if not host:
        return jsonify({"ok": False, "note": "адрес не передан"})

    values = {"gateway_host": host}
    if port:
        values["gateway_port"] = port
    db.save_settings(values)

    ok, note = gw.from_settings(db.get_settings()).health()
    return jsonify({"ok": ok, "note": note, "host": host})


@app.route("/settings/usb-status", methods=["POST"])
def settings_usb_status():
    settings = db.get_settings()
    return jsonify(usb.status(settings.get("usb_local_port")))


@app.route("/settings/usb-connect", methods=["POST"])
def settings_usb_connect():
    """Пробрасывает порт шлюза через кабель и переключает панель в режим USB."""
    settings = db.get_settings()
    local_port = int(settings.get("usb_local_port", 18080) or 18080)
    remote_port = int(settings.get("gateway_port", 8080) or 8080)

    try:
        device = usb.ensure_forward(local_port, remote_port)
    except usb.UsbError as exc:
        return jsonify({"ok": False, "note": exc.full()})

    db.save_settings({"connection_mode": "usb"})
    ok, note = gw.from_settings(db.get_settings()).health()

    label = device.get("model") or device.get("serial")
    return jsonify({
        "ok": ok,
        "device": label,
        "note": "{}: {} → порт {} телефона проброшен на 127.0.0.1:{}".format(
            label, note, remote_port, local_port),
    })


@app.route("/settings/test-sms", methods=["POST"])
def settings_test_sms():
    settings = db.get_settings()
    phone, error = phones.normalize(request.form.get("phone"),
                                    settings["default_country_code"])
    if error:
        flash("Номер не принят: {}".format(error), "error")
        return redirect(url_for("settings_page"))

    text = request.form.get("text", "").strip() or "Тестовое сообщение"
    try:
        gateway_id, state = gw.from_settings(settings).send(
            phone, text, sim_number=int(settings.get("sim_number", 0))
        )
        flash("Отправлено на {} (id шлюза {}, статус {})".format(phone, gateway_id, state),
              "ok")
    except gw.GatewayError as exc:
        flash("Не отправлено: {}".format(exc.full()), "error")
    return redirect(url_for("settings_page"))


if __name__ == "__main__":
    print("Панель рассылки: http://{}:{}".format(config.FLASK_HOST, config.FLASK_PORT))
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT,
            threaded=True, use_reloader=False)
