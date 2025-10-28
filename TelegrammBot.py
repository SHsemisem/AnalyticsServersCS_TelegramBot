import asyncio
import html
import io
import os
import re
import json
import datetime as dt
from dataclasses import dataclass, field
from ftplib import FTP
from collections import Counter
from statistics import fmean
from types import SimpleNamespace
from typing import List

import pytz
import matplotlib
matplotlib.use("Agg")  # без GUI
import matplotlib.pyplot as plt

from dotenv import load_dotenv
from telegram import (
    InputFile,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------- Настройки из .env ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID"))
FTP_HOST = os.getenv("FTP_HOST")
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
FTP_PATH = os.getenv("FTP_PATH", "/logs/mapstats_YYYY-MM-DD.log")
FTP_TIMEOUT = float(os.getenv("FTP_TIMEOUT", "30"))
FTP_PASSIVE = os.getenv("FTP_PASSIVE", "false").lower() in {"1", "true", "yes"}
SERVER_NAME = os.getenv("SERVER_NAME", "default")
SERVERS_FILE = os.getenv("SERVERS_FILE", "servers.json")
TZNAME = os.getenv("TIMEZONE", "Europe/Kyiv")
TZ = pytz.timezone(TZNAME)
REPORT_TIME = os.getenv("REPORT_TIME", "23:00")
REPORT_TYPE = os.getenv("REPORT_TYPE", "mapstats")

# ---------- Разбор нашего формата лога ----------
# Пример блока:
# [2025-10-19 00:04:12] Карта: awp_india
#   Уникальных игроков: 22
#   ...
#   Средний онлайн: 21 игроков
#   ...
# ----------------------------------------

RE_HEAD = re.compile(r"\[(?P<dt>[\d\-: ]+)\]\s+Карта:\s+(?P<map>.+)")
RE_AVG = re.compile(r"Средний онлайн:\s+(?P<avg>\d+)\s+игрок")
RE_ANALYZER_LINE = re.compile(
    r"^L (?P<date>\d{2}/\d{2}/\d{4}) - (?P<time>\d{2}:\d{2}:\d{2}): (?P<body>.+)$"
)

DAY_RANGE = ("07:00", "23:00")
EVENING_RANGE = ("18:00", "23:00")
NIGHT_RANGE = ("23:00", "07:00")

REPORT_TIME_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")

VALID_REPORT_TYPES = {"mapstats", "analyzer"}

CHOOSE_DATE, CUSTOM_DATE_INPUT, CHOOSE_SERVER, CHOOSE_REPORT = range(4)
LAST_CHOICES_KEY = "last_user_choices"

HELP_MESSAGE = (
    "👋 Этот бот собирает отчеты по серверам CSMOV.\n\n"
    "• /start — открыть меню выбора отчета.\n"
    "• /cancel — прервать текущий диалог.\n"
    "• /help — показать эту подсказку.\n\n"
    "Как работает меню:\n"
    "1. Выберите дату (сегодня, вчера, позавчера или укажите вручную).\n"
    "2. Выберите сервер — можно сразу для всех.\n"
    "3. Выберите тип отчета: графики (mapstats), таблица мониторингов (analyzer) или оба.\n"
    "4. Получите отчеты в чат. Бот запомнит выбор и в следующий раз предложит повторить его одной кнопкой.\n\n"
    "Совет: если сообщение прервалось, отправьте /start и начните заново."
)


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_report_time(value: str, tz: pytz.BaseTzInfo) -> dt.time:
    match = REPORT_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"REPORT_TIME '{value}' должен быть в формате HH:MM")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"REPORT_TIME '{value}' содержит некорректные часы или минуты")
    return dt.time(hour=hour, minute=minute, tzinfo=tz)


@dataclass
class AnalyzerEntry:
    timestamp: dt.datetime
    source: str
    steam_id: str
    ip: str
    nickname: str


@dataclass
class AnalyzerSummary:
    source: str
    hits: int
    unique_steam: int
    unique_ips: int
    last_seen: dt.datetime


@dataclass
class ReportConfig:
    report_type: str
    path: str
    name: str | None = None
    passive: bool | None = None
    timeout: float | None = None

    @classmethod
    def from_dict(
        cls,
        data: dict,
        fallback_type: str,
        fallback_path: str | None,
    ) -> "ReportConfig":
        report_type_raw = data.get("type", fallback_type)
        if not report_type_raw:
            raise ValueError("Для отчета необходимо указать 'type'")
        report_type = str(report_type_raw).lower()
        if report_type not in VALID_REPORT_TYPES:
            raise ValueError(
                f"type отчета должен быть одним из {', '.join(sorted(VALID_REPORT_TYPES))}"
            )

        path = data.get("ftp_path", fallback_path)
        if not path:
            raise ValueError("Для отчета необходимо указать 'ftp_path'")

        name = data.get("name")
        passive_value = data.get("ftp_passive")
        passive = None if passive_value is None else _coerce_bool(passive_value)
        timeout = data.get("ftp_timeout")
        timeout_value = float(timeout) if timeout is not None else None

        return cls(
            report_type=report_type,
            path=path,
            name=name,
            passive=passive,
            timeout=timeout_value,
        )

    def resolved_passive(self, server_default: bool) -> bool:
        return self.passive if self.passive is not None else server_default

    def resolved_timeout(self, server_default: float) -> float:
        return self.timeout if self.timeout is not None else server_default

    def build_path(self, for_date: dt.date) -> str:
        return (
            self.path.replace("YYYY", f"{for_date:%Y}")
            .replace("MM", f"{for_date:%m}")
            .replace("DD", f"{for_date:%d}")
        )


@dataclass
class ServerConfig:
    name: str
    host: str
    user: str
    password: str
    passive: bool = False
    timeout: float | None = None
    reports: List[ReportConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ServerConfig":
        try:
            name = data["name"]
            host = data["ftp_host"]
            user = data["ftp_user"]
            password = data["ftp_pass"]
        except KeyError as missing:
            raise ValueError(f"Отсутствует ключ {missing} в описании сервера") from None

        timeout = data.get("ftp_timeout")
        timeout_value = float(timeout) if timeout is not None else None
        passive = _coerce_bool(data.get("ftp_passive", False))

        fallback_type = str(data.get("report_type", "mapstats")).lower()
        if fallback_type not in VALID_REPORT_TYPES:
            raise ValueError(
                f"report_type должен быть одним из {', '.join(sorted(VALID_REPORT_TYPES))}"
            )
        fallback_path = data.get("ftp_path")

        reports_payload = data.get("reports")
        reports: List[ReportConfig]
        if reports_payload is not None:
            if not isinstance(reports_payload, list) or not reports_payload:
                raise ValueError(
                    "Поле 'reports' должно быть непустым списком описаний отчетов"
                )
            reports = [
                ReportConfig.from_dict(item, fallback_type, fallback_path)
                for item in reports_payload
            ]
        else:
            if not fallback_path:
                raise ValueError(
                    "Не указан ftp_path для сервера и отсутствует список reports"
                )
            reports = [
                ReportConfig(
                    report_type=fallback_type,
                    path=fallback_path,
                    name=data.get("report_name"),
                )
            ]

        return cls(
            name=name,
            host=host,
            user=user,
            password=password,
            passive=passive,
            timeout=timeout_value,
            reports=reports,
        )

    def resolved_timeout(self) -> float:
        return self.timeout if self.timeout is not None else FTP_TIMEOUT

    def resolved_passive(self) -> bool:
        return self.passive

def load_servers() -> List[ServerConfig]:
    servers_path = SERVERS_FILE
    if servers_path and os.path.exists(servers_path):
        with open(servers_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, list):
            raise ValueError("Файл с серверами должен содержать список объектов")
        servers = [ServerConfig.from_dict(item) for item in payload]
        if not servers:
            raise ValueError("Список серверов пуст — добавьте хотя бы один сервер")
        return servers

    if all([FTP_HOST, FTP_USER, FTP_PASS]):
        report_type_env = REPORT_TYPE.lower()
        if report_type_env not in VALID_REPORT_TYPES:
            raise ValueError(
                f"REPORT_TYPE должен быть одним из {', '.join(sorted(VALID_REPORT_TYPES))}"
            )
        report = ReportConfig(
            report_type=report_type_env,
            path=FTP_PATH,
            name=os.getenv("REPORT_NAME"),
            passive=None,
            timeout=None,
        )
        return [
            ServerConfig(
                name=SERVER_NAME,
                host=FTP_HOST,
                user=FTP_USER,
                password=FTP_PASS,
                passive=FTP_PASSIVE,
                timeout=FTP_TIMEOUT,
                reports=[report],
            )
        ]

    raise RuntimeError(
        "Не найдено описание серверов. Укажите SERVERS_FILE с перечнем серверов "
        "или заполните FTP_HOST/FTP_USER/FTP_PASS в .env"
    )


SERVERS = load_servers()

def _time_to_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _minutes_of(dt_obj: dt.datetime) -> int:
    return dt_obj.hour * 60 + dt_obj.minute


def _filter_points(points, start: str, end: str):
    start_min = _time_to_minutes(start)
    end_min = _time_to_minutes(end)
    for point in points:
        minutes = _minutes_of(point[0])
        if start_min <= end_min:
            if start_min <= minutes <= end_min:
                yield point
        else:  # охватываем переход через полночь
            if minutes >= start_min or minutes <= end_min:
                yield point


def _average_online(points, start: str, end: str):
    selected = [avg for _, avg, _ in _filter_points(points, start, end)]
    return fmean(selected) if selected else None


def _map_with_highest_average(points, start: str, end: str):
    buckets = {}
    for _, avg, map_name in _filter_points(points, start, end):
        buckets.setdefault(map_name, []).append(avg)
    if not buckets:
        return None
    def score(item):
        name, values = item
        return (fmean(values), len(values))
    best_name, _ = max(buckets.items(), key=score)
    return best_name


def _map_with_deepest_drop(points, start: str, end: str):
    selected = list(_filter_points(points, start, end))
    if len(selected) < 2:
        return None
    worst_drop = None
    worst_map = None
    previous_avg = None
    for _, avg, map_name in selected:
        if previous_avg is not None:
            delta = avg - previous_avg
            if delta < 0 and (worst_drop is None or delta < worst_drop):
                worst_drop = delta
                worst_map = map_name
        previous_avg = avg
    return worst_map


def parse_mapstats_log(text: str, tz: pytz.BaseTzInfo):
    points = []  # (time, avg_online, map_name)
    maps_order = []  # для легенды справа
    for block in text.strip().split("----------------------------------------"):
        block = block.strip()
        if not block:
            continue
        head = RE_HEAD.search(block)
        avgm = RE_AVG.search(block)
        if not head or not avgm:
            continue
        dt_str = head.group("dt").strip()   # '2025-10-19 00:04:12'
        map_name = head.group("map").strip()
        avg = int(avgm.group("avg"))
        naive = dt.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        aware = pytz.utc.localize(naive) if naive.tzinfo is None else naive
        # Если в файле локальное время — уберите utc.localize и сделайте tz.localize
        # aware = tz.localize(naive)
        points.append((aware.astimezone(tz), avg, map_name))
        maps_order.append(map_name)

    points.sort(key=lambda x: x[0])
    # Пронумеруем карты по порядку появления
    order_unique = []
    seen = set()
    for m in maps_order:
        if m not in seen:
            seen.add(m)
            order_unique.append(m)
    map_index = {m: i+1 for i, m in enumerate(order_unique)}  # 1..N

    return points, order_unique, map_index


def _localize_with_tz(naive: dt.datetime, tz: pytz.BaseTzInfo) -> dt.datetime:
    if naive.tzinfo is not None:
        return naive.astimezone(tz)
    try:
        return tz.localize(naive, is_dst=None)
    except Exception:
        # fallback для неоднозначных дат (переход на летнее/зимнее время)
        return tz.localize(naive, is_dst=True)


def parse_analyzer_log(text: str, tz: pytz.BaseTzInfo) -> List[AnalyzerEntry]:
    entries: List[AnalyzerEntry] = []
    for raw_line in text.splitlines():
        raw_line = raw_line.rstrip()
        match = RE_ANALYZER_LINE.match(raw_line)
        if not match:
            continue
        body = match.group("body").strip()
        columns = re.split(r"\s{2,}", body)
        if len(columns) < 4:
            continue
        date_part = match.group("date")
        time_part = match.group("time")
        naive = dt.datetime.strptime(f"{date_part} {time_part}", "%m/%d/%Y %H:%M:%S")
        aware = _localize_with_tz(naive, tz)
        source = columns[0].strip()
        steam_id = columns[1].strip()
        ip_addr = columns[2].strip()
        nickname = "  ".join(part.strip() for part in columns[3:])
        entries.append(
            AnalyzerEntry(
                timestamp=aware,
                source=source,
                steam_id=steam_id,
                ip=ip_addr,
                nickname=nickname,
            )
        )
    return entries


def aggregate_analyzer_entries(
    entries: List[AnalyzerEntry], report_date: dt.date, tz: pytz.BaseTzInfo
) -> tuple[List[AnalyzerSummary], int]:
    day_entries = [
        entry for entry in entries if entry.timestamp.astimezone(tz).date() == report_date
    ]
    if not day_entries:
        return [], 0

    buckets = {}
    for entry in day_entries:
        stats = buckets.setdefault(
            entry.source,
            {
                "count": 0,
                "steam": set(),
                "ips": set(),
                "last_seen": entry.timestamp,
            },
        )
        stats["count"] += 1
        stats["steam"].add(entry.steam_id)
        stats["ips"].add(entry.ip)
        if entry.timestamp > stats["last_seen"]:
            stats["last_seen"] = entry.timestamp

    summaries = [
        AnalyzerSummary(
            source=source,
            hits=data["count"],
            unique_steam=len(data["steam"]),
            unique_ips=len(data["ips"]),
            last_seen=data["last_seen"],
        )
        for source, data in buckets.items()
    ]
    summaries.sort(key=lambda item: (-item.hits, item.source.lower()))
    return summaries, len(day_entries)


def format_analyzer_table(
    summaries: List[AnalyzerSummary], tz: pytz.BaseTzInfo
) -> str:
    if not summaries:
        return ""

    headers = ("site", "count")
    rows = []
    for summary in summaries:
        rows.append(
            [
                summary.source,
                str(summary.hits),
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def render_row(values):
        formatted = []
        last_idx = len(values) - 1
        for idx, value in enumerate(values):
            if idx == last_idx:
                formatted.append(value.rjust(widths[idx]))
            else:
                formatted.append(value.ljust(widths[idx]))
        return "  ".join(formatted)

    lines = [render_row(headers)]
    lines.extend(render_row(row) for row in rows)
    table = "\n".join(lines)
    return table

# ---------- Построение графика ----------
def make_plot(points, order_unique, map_index, date_for_title: dt.date) -> bytes:
    if not points:
        raise ValueError("Нет данных для построения графика")

    times = [p[0] for p in points]
    avgs  = [p[1] for p in points]
    maps  = [p[2] for p in points]

    fig, ax = plt.subplots(figsize=(12, 8), dpi=160, layout="constrained")

    ax.plot(times, avgs, marker="x", linestyle="-", linewidth=1.2)
    ax.set_title(f"Дневной онлайн за {date_for_title:%Y-%m-%d} (номер = карта из списка)", fontsize=12)
    ax.set_xlabel("Время записи (ЧЧ:ММ)")
    ax.set_ylabel("Средний онлайн (игроков)")

    # сетка и формат осей
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate(rotation=45)

    # Точки подписываем индексами карты
    for t, y, m in zip(times, avgs, maps):
        ax.annotate(str(map_index[m]), (t, y), textcoords="offset points", xytext=(0, 6), ha="center", fontsize=7)

    # Легенда справа: индекс -> карта
    legend_text = "\n".join(f"{i+1}: {name}" for i, name in enumerate(order_unique))
    bbox = dict(boxstyle="round", facecolor="white", alpha=0.85)
    ax.text(1.02, 0.5, legend_text, transform=ax.transAxes, fontsize=8,
            va="center", ha="left", bbox=bbox)

    # Немного статистики внизу
    avg_all = sum(avgs) / len(avgs)
    max_online = max(avgs)
    max_idx = avgs.index(max_online)
    max_time = times[max_idx]
    max_map = maps[max_idx]

    counts_by_map = Counter(maps)
    most_common_map, _ = counts_by_map.most_common(1)[0]

    avg_day = _average_online(points, *DAY_RANGE)
    avg_evening = _average_online(points, *EVENING_RANGE)
    avg_night = _average_online(points, *NIGHT_RANGE)
    popular_day_map = _map_with_highest_average(points, *DAY_RANGE)
    drop_day_map = _map_with_deepest_drop(points, *DAY_RANGE)

    summary_lines = [f"Средний онлайн за день — {avg_all:.1f}"]
    if avg_day is not None:
        summary_lines.append(f"[ЦЕЛЫЙ ДЕНЬ] Средний онлайн с {DAY_RANGE[0]} до {DAY_RANGE[1]} — {avg_day:.1f}")
    if avg_evening is not None:
        summary_lines.append(f"[ВЕЧЕР] Средний онлайн с {EVENING_RANGE[0]} до {EVENING_RANGE[1]} — {avg_evening:.1f}")
    if avg_night is not None:
        summary_lines.append(f"[НОЧЬ] Средний онлайн с {NIGHT_RANGE[0]} до {NIGHT_RANGE[1]} — {avg_night:.1f}")
    if popular_day_map:
        summary_lines.append(f"Самая популярная карта по онлайне (днём) — {popular_day_map}")
    if drop_day_map:
        summary_lines.append(f"Карта с самым сильным падением онлайна (днём) — {drop_day_map}")
    summary_lines.append(f"Пик онлайна — {max_online} на карте {max_map} в {max_time:%H:%M}")
    summary_lines.append(f"Чаще остальных встречалась карта — {most_common_map}")

    footer = "\n".join(summary_lines)
    fig.text(0.02, -0.12, footer, fontsize=9, transform=ax.transAxes, va="top")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ---------- FTP ----------
def fetch_log_from_ftp(server: ServerConfig, report: ReportConfig, for_date: dt.date) -> str:
    path = report.build_path(for_date)
    timeout_default = server.resolved_timeout()
    timeout = report.resolved_timeout(timeout_default)
    passive = report.resolved_passive(server.resolved_passive())
    with FTP(timeout=timeout) as ftp:
        ftp.connect(server.host)
        ftp.login(server.user, server.password)
        if passive:
            ftp.set_pasv(True)
        if ftp.sock is not None:
            ftp.sock.settimeout(timeout)
        ftp.sendcmd("TYPE I")  # binary mode, чтобы сервер разрешил SIZE/RETR
        buffer = bytearray()
        ftp.retrbinary(f"RETR {path}", buffer.extend)

    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            return buffer.decode(encoding)
        except UnicodeDecodeError:
            continue
    return buffer.decode("utf-8", errors="ignore")

# ---------- Отправка отчета ----------
def _report_display_name_plain(server: ServerConfig, report: ReportConfig) -> str:
    if report.name:
        return f"{server.name} • {report.name}"
    if report.report_type == "mapstats":
        return server.name
    return f"{server.name} • {report.report_type}"


def _report_display_name_html(server: ServerConfig, report: ReportConfig) -> str:
    base = html.escape(server.name)
    if report.name:
        return f"{base} • {html.escape(report.name)}"
    if report.report_type == "mapstats":
        return base
    return f"{base} • {html.escape(report.report_type)}"


def _report_file_stub(server: ServerConfig, report: ReportConfig) -> str:
    parts = [server.name]
    label = report.name or (report.report_type if report.report_type != "mapstats" else "")
    if label:
        parts.append(label)
    safe_parts = [re.sub(r"[^A-Za-z0-9_.-]+", "_", part.strip()) for part in parts if part]
    return "_".join(filter(None, safe_parts))


async def send_report(
    server: ServerConfig,
    report: ReportConfig,
    report_date: dt.date,
    bot,
    target_chat_id: int,
):
    log_text = fetch_log_from_ftp(server, report, report_date)
    if report.report_type == "analyzer":
        entries = parse_analyzer_log(log_text, TZ)
        summaries, total_events = aggregate_analyzer_entries(entries, report_date, TZ)
        if not summaries:
            await bot.send_message(
                chat_id=target_chat_id,
                text=(
                    f"[{_report_display_name_plain(server, report)}] "
                    f"Нет записей analyzer за {report_date:%Y-%m-%d}"
                ),
            )
            return
        table = format_analyzer_table(summaries, TZ)
        header_plain = (
            f"[{_report_display_name_plain(server, report)}] "
            f"Мониторы за {report_date:%Y-%m-%d}"
        )
        summary_plain = (
            f"Всего событий: {total_events}, источников: {len(summaries)}"
        )
        header_html = (
            f"[{_report_display_name_html(server, report)}] "
            f"Мониторы за {report_date:%Y-%m-%d}"
        )
        summary_html = html.escape(summary_plain)

        table_html = html.escape(table)
        message_body = (
            f"{header_html}\n{summary_html}\n<pre>{table_html}</pre>"
        )
        if len(message_body) <= 3500:
            await bot.send_message(
                chat_id=target_chat_id,
                text=message_body,
                parse_mode="HTML",
            )
        else:
            file_stub = _report_file_stub(server, report)
            if not file_stub:
                file_stub = "report"
            buffer = io.BytesIO()
            buffer.write(table.encode("utf-8"))
            buffer.seek(0)
            await bot.send_document(
                chat_id=target_chat_id,
                document=InputFile(
                    buffer,
                    filename=f"{file_stub}_analyzer_{report_date}.txt",
                ),
                caption=f"{header_plain}\n{summary_plain}",
            )
        return

    if report.report_type == "mapstats":
        points, order_unique, map_index = parse_mapstats_log(log_text, TZ)
        img_bytes = make_plot(points, order_unique, map_index, report_date)

        caption = (
            f"[{_report_display_name_plain(server, report)}] "
            f"Отчет за {report_date:%Y-%m-%d}"
        )
        file_stub = _report_file_stub(server, report)
        if not file_stub:
            file_stub = "report"
        await bot.send_photo(
            chat_id=target_chat_id,
            photo=InputFile(
                io.BytesIO(img_bytes),
                filename=f"{file_stub}_online_{report_date}.png",
            ),
            caption=caption,
        )
        return

    raise ValueError(f"Неизвестный тип отчета: {report.report_type}")

# ---------- Job: основной ежедневный цикл ----------
async def daily_job(context):
    # Берём «вчера», если вы генерируете отчет по прошедшему дню поздно вечером
    now = dt.datetime.now(TZ)
    report_date = now.date()
    for server in SERVERS:
        for report in server.reports:
            try:
                await send_report(server, report, report_date, context.bot, CHAT_ID)
            except Exception as e:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"❌ [{_report_display_name_plain(server, report)}] "
                        f"Ошибка отчета: {e}"
                    ),
                )


def _build_date_keyboard(has_last: bool) -> InlineKeyboardMarkup:
    keyboard = []
    if has_last:
        keyboard.append(
            [InlineKeyboardButton("Повторить предыдущий отчет", callback_data="repeat:last")]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton("Сегодня", callback_data="date:today"),
                InlineKeyboardButton("Вчера", callback_data="date:yesterday"),
            ],
            [
                InlineKeyboardButton("Позавчера", callback_data="date:before_yesterday"),
                InlineKeyboardButton("Выбрать дату", callback_data="date:custom"),
            ],
            [InlineKeyboardButton("Отмена", callback_data="date:cancel")],
        ]
    )
    return InlineKeyboardMarkup(keyboard)


def _build_server_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("Все серверы", callback_data="server:all")]]
    for idx, server in enumerate(SERVERS):
        keyboard.append(
            [InlineKeyboardButton(server.name, callback_data=f"server:{idx}")]
        )
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="server:cancel")])
    return InlineKeyboardMarkup(keyboard)


def _build_report_type_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Все типы", callback_data="type:all")],
        [
            InlineKeyboardButton("Mapstats", callback_data="type:mapstats"),
            InlineKeyboardButton("Analyzer", callback_data="type:analyzer"),
        ],
        [InlineKeyboardButton("Отмена", callback_data="type:cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)


def _format_date_for_prompt(value: dt.date) -> str:
    return value.strftime("%Y-%m-%d")


def _reset_conversation_state(user_data: dict):
    for key in ("selected_date", "selected_server", "selected_report_type"):
        user_data.pop(key, None)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _reset_conversation_state(context.user_data)
    user = update.effective_user
    user_id = user.id if user else None
    last_choices = context.application.bot_data.get(LAST_CHOICES_KEY, {})
    has_last = bool(user_id is not None and user_id in last_choices)
    markup = _build_date_keyboard(has_last)
    text = (
        "Выберите дату отчета.\n"
        "Подсказка: /help расскажет о доступных командах."
    )
    if has_last:
        text += "\nИли нажмите 'Повторить предыдущий отчет' выше."
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    else:
        query = update.callback_query
        if query:
            await query.answer()
            await query.edit_message_text(text, reply_markup=markup)
    return CHOOSE_DATE


async def handle_repeat_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data.startswith("repeat:"):
        return CHOOSE_DATE
    await query.answer()
    user = query.from_user
    user_id = user.id if user else None
    if user_id is None:
        await query.edit_message_text(
            "Не удалось определить пользователя. Попробуйте снова с /start."
        )
        return ConversationHandler.END

    payload = context.application.bot_data.get(LAST_CHOICES_KEY, {}).get(user_id)
    if not payload:
        await query.edit_message_text(
            "Повторять пока нечего. Выполните отчет через меню и попробуйте снова."
        )
        return ConversationHandler.END

    try:
        selected_date = dt.date.fromisoformat(payload["date"])
    except Exception:
        await query.edit_message_text(
            "Не удалось восстановить сохраненную дату. Запросите отчет заново через меню."
        )
        return ConversationHandler.END

    context.user_data["selected_date"] = selected_date
    context.user_data["selected_server"] = payload.get("server", "all")
    context.user_data["selected_report_type"] = payload.get("type", "all")

    await query.edit_message_text("Повторяю предыдущий отчет, пожалуйста подождите...")
    await run_manual_reports(user_id, query.message.chat_id, context)
    _reset_conversation_state(context.user_data)
    return ConversationHandler.END


async def handle_date_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data.startswith("date:"):
        return CHOOSE_DATE
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        _reset_conversation_state(context.user_data)
        await query.edit_message_text(
            "Диалог отменен. Чтобы начать заново, отправьте /start."
        )
        return ConversationHandler.END

    today = dt.datetime.now(TZ).date()
    if choice == "today":
        selected_date = today
    elif choice == "yesterday":
        selected_date = today - dt.timedelta(days=1)
    elif choice == "before_yesterday":
        selected_date = today - dt.timedelta(days=2)
    elif choice == "custom":
        await query.edit_message_text("Введите дату в формате YYYY-MM-DD:")
        return CUSTOM_DATE_INPUT
    else:
        await query.edit_message_text("Неизвестный выбор. Попробуйте снова с /start.")
        return ConversationHandler.END

    context.user_data["selected_date"] = selected_date
    markup = _build_server_keyboard()
    await query.edit_message_text(
        (
            f"Выберите сервер для отчета за {_format_date_for_prompt(selected_date)}.\n"
            "Можно отправить отчеты по всем серверам или выбрать конкретный.\n"
            "Для отмены используйте кнопку 'Отмена' ниже или /cancel."
        ),
        reply_markup=markup,
    )
    return CHOOSE_SERVER


async def handle_custom_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return CUSTOM_DATE_INPUT
    text = (message.text or "").strip()
    try:
        selected_date = dt.date.fromisoformat(text)
    except ValueError:
        await message.reply_text(
            "Не удалось распознать дату. Введите значение в формате YYYY-MM-DD:"
        )
        return CUSTOM_DATE_INPUT

    context.user_data["selected_date"] = selected_date
    markup = _build_server_keyboard()
    await message.reply_text(
        (
            f"Выберите сервер для отчета за {_format_date_for_prompt(selected_date)}.\n"
            "Можно отправить отчеты по всем серверам или выбрать конкретный.\n"
            "Для отмены используйте кнопку 'Отмена' ниже или /cancel."
        ),
        reply_markup=markup,
    )
    return CHOOSE_SERVER


async def handle_server_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data.startswith("server:"):
        return CHOOSE_SERVER
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        _reset_conversation_state(context.user_data)
        await query.edit_message_text(
            "Диалог отменен. Чтобы начать заново, отправьте /start."
        )
        return ConversationHandler.END

    context.user_data["selected_server"] = choice
    markup = _build_report_type_keyboard()
    selected_date = context.user_data.get("selected_date")
    date_display = (
        _format_date_for_prompt(selected_date) if isinstance(selected_date, dt.date) else ""
    )
    await query.edit_message_text(
        (
            f"Выберите тип отчета за {date_display}.\n"
            "Mapstats — график онлайна, Analyzer — таблица мониторингов, 'Все типы' отправит оба.\n"
            "Для отмены используйте кнопку 'Отмена' ниже или /cancel."
        ),
        reply_markup=markup,
    )
    return CHOOSE_REPORT


async def handle_report_type_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not query or not query.data.startswith("type:"):
        return CHOOSE_REPORT
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        _reset_conversation_state(context.user_data)
        await query.edit_message_text(
            "Диалог отменен. Чтобы начать заново, отправьте /start."
        )
        return ConversationHandler.END

    context.user_data["selected_report_type"] = choice
    await query.edit_message_text("Формирую отчеты...")
    user = query.from_user
    user_id = user.id if user else None
    await run_manual_reports(user_id, query.message.chat_id, context)
    _reset_conversation_state(context.user_data)
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _reset_conversation_state(context.user_data)
    if update.message:
        await update.message.reply_text(
            "Диалог отменен. Чтобы начать заново, отправьте /start."
        )
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "Диалог отменен. Чтобы начать заново, отправьте /start."
        )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(HELP_MESSAGE)
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(HELP_MESSAGE)
    else:
        chat = update.effective_chat
        if chat:
            await context.bot.send_message(chat_id=chat.id, text=HELP_MESSAGE)


async def run_manual_reports(
    user_id: int | None, chat_id: int, context: ContextTypes.DEFAULT_TYPE
):
    selected_date = context.user_data.get("selected_date")
    selected_server = context.user_data.get("selected_server")
    report_type = context.user_data.get("selected_report_type")

    if not isinstance(selected_date, dt.date) or selected_server is None or not report_type:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Недостаточно данных для формирования отчета. Попробуйте снова с /start.",
        )
        return

    if selected_server == "all":
        servers = SERVERS
    else:
        try:
            index = int(selected_server)
            servers = [SERVERS[index]]
        except (ValueError, IndexError):
            await context.bot.send_message(
                chat_id=chat_id,
                text="Указан неизвестный сервер. Попробуйте снова с /start.",
            )
            return

    report_pairs = []
    for server in servers:
        if report_type == "all":
            filtered = server.reports
        else:
            filtered = [item for item in server.reports if item.report_type == report_type]
        for item in filtered:
            report_pairs.append((server, item))

    if not report_pairs:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Нет отчетов, соответствующих выбранным параметрам.",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"Готовлю {len(report_pairs)} отчет(ов) за "
            f"{_format_date_for_prompt(selected_date)}..."
        ),
    )

    for server, report in report_pairs:
        try:
            await send_report(server, report, selected_date, context.bot, chat_id)
        except Exception as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ [{_report_display_name_plain(server, report)}] "
                    f"Ошибка отчета: {e}"
                ),
            )

    await context.bot.send_message(
        chat_id=chat_id,
        text="Готово. Чтобы запросить новые данные, отправьте /start.",
    )

    if user_id is not None:
        store = context.application.bot_data.setdefault(LAST_CHOICES_KEY, {})
        store[user_id] = {
            "date": selected_date.isoformat(),
            "server": selected_server,
            "type": report_type,
        }

# ---------- Запуск бота ----------
async def _schedule_reports(app: Application):
    while True:
        run_time = parse_report_time(REPORT_TIME, TZ)
        now = dt.datetime.now(TZ)
        target = dt.datetime.combine(now.date(), run_time)
        if target <= now:
            target += dt.timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        context = SimpleNamespace(bot=app.bot)
        await daily_job(context)


async def _start_scheduler(app: Application):
    asyncio.create_task(_schedule_reports(app))


async def _configure_bot(app: Application):
    commands = [
        BotCommand("start", "Открыть меню отчетов"),
        BotCommand("help", "Показать подсказку"),
        BotCommand("cancel", "Прервать текущий диалог"),
    ]
    await app.bot.set_my_commands(commands)
    try:
        await app.bot.set_chat_menu_button(MenuButtonCommands())
    except Exception:
        pass


async def _post_init(app: Application):
    await _configure_bot(app)
    await _start_scheduler(app)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    menu_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            CHOOSE_DATE: [
                CallbackQueryHandler(handle_repeat_choice, pattern=r"^repeat:"),
                CallbackQueryHandler(handle_date_choice, pattern=r"^date:"),
            ],
            CUSTOM_DATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_date_input)],
            CHOOSE_SERVER: [CallbackQueryHandler(handle_server_choice, pattern=r"^server:")],
            CHOOSE_REPORT: [CallbackQueryHandler(handle_report_type_choice, pattern=r"^type:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="manual_report_menu",
        persistent=False,
    )
    app.add_handler(menu_handler)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    # Можно оставить polling (бот не обязан принимать команды)
    app.run_polling()

if __name__ == "__main__":
    main()
