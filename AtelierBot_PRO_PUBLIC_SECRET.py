import os
import json
import html
import logging
import re
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# ATELIERBOT — VERSION PRO
# - Stock / mouvements
# - Commandes / livraisons
# - Réparations
# - Fournisseurs
# - Recherche globale
# - Statistiques
# - Collaborateurs persistants
# - Journal d'activité
# - JSON atomique + sauvegardes
#
# Sécurité :
#   BOT_TOKEN       = token Telegram
#   ATELIER_PASSWORD = mot de passe collaborateurs
#
# Ne mets PAS ces secrets dans GitHub en clair.
# ============================================================

ADMIN_CHAT_ID = 7919387190
DATA_FILE = Path(__file__).with_name("data.json")
BACKUP_DIR = Path(__file__).with_name("backups")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SCANNER_WEBAPP_URL = "https://idrisse7.github.io/AtelierGestionBot1/"
ATELIER_PASSWORD = os.getenv("ATELIER_PASSWORD", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("AtelierBot")

# Conversation states
SEARCH, ADD_STOCK, ADD_SUPPLIER, ADD_ORDER, ADD_DELIVERY, ADD_REPAIR = range(6)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_db() -> dict[str, Any]:
    return {
        "version": 1,
        "settings": {
            "atelier_name": "AtelierBot",
            "low_stock_default": 2,
        },
        "users": {
            str(ADMIN_CHAT_ID): {
                "role": "admin",
                "name": "Administrateur",
                "added_at": now_iso(),
            }
        },
        "stock": [],
        "appareils": [],
        "commandes": [],
        "livraisons": [],
        "reparations": [],
        "fournisseurs": [],
        "mouvements": [],
        "activity_log": [],
        "deblocages": [],
    }


def save_db(db: dict[str, Any], make_backup: bool = True) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    if make_backup and DATA_FILE.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"data_{stamp}.json"
        try:
            shutil.copy2(DATA_FILE, backup)
            backups = sorted(BACKUP_DIR.glob("data_*.json"))
            for old in backups[:-20]:
                old.unlink(missing_ok=True)
        except OSError:
            log.warning("Sauvegarde locale impossible", exc_info=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix="atelier_", suffix=".json", dir=str(DATA_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, DATA_FILE)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def load_db() -> dict[str, Any]:
    if not DATA_FILE.exists():
        db = default_db()
        save_db(db)
        return db

    try:
        # Une ancienne exécution peut avoir laissé un data.json vide.
        # Dans ce cas, on repart proprement avec la structure par défaut.
        if DATA_FILE.stat().st_size == 0:
            log.warning("data.json est vide : initialisation de la base par défaut")
            db = default_db()
            save_db(db, make_backup=False)
            return db
        with DATA_FILE.open("r", encoding="utf-8") as f:
            db = json.load(f)
    except (json.JSONDecodeError, OSError):
        log.exception("Impossible de lire data.json")
        raise

    if not isinstance(db, dict):
        db = default_db()

    template = default_db()
    for key, value in template.items():
        db.setdefault(key, value)

    db.setdefault("users", {})
    db.setdefault("appareils", [])
    db["users"].setdefault(
        str(ADMIN_CHAT_ID),
        {"role": "admin", "name": "Administrateur", "added_at": now_iso()},
    )
    return db


DB = load_db()



def user_record(chat_id: int) -> dict[str, Any] | None:
    return DB.get("users", {}).get(str(chat_id))


def authorized(chat_id: int | None) -> bool:
    if chat_id is None:
        return False
    return str(chat_id) in DB.get("users", {})


def admin(chat_id: int | None) -> bool:
    rec = user_record(chat_id) if chat_id is not None else None
    return bool(rec and rec.get("role") == "admin") or chat_id == ADMIN_CHAT_ID


def moderator(chat_id: int | None) -> bool:
    rec = user_record(chat_id) if chat_id is not None else None
    return bool(rec and rec.get("role") == "moderateur")


def can_manage_users(chat_id: int | None) -> bool:
    return admin(chat_id) or moderator(chat_id)


def log_activity(chat_id: int, action: str, details: str = "") -> None:
    DB.setdefault("activity_log", []).append({
        "date": now_iso(),
        "chat_id": chat_id,
        "action": action,
        "details": details[:500],
    })
    DB["activity_log"] = DB["activity_log"][-500:]
    save_db(DB)


def money(v: Any) -> str:
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",") + " €"
    except (TypeError, ValueError):
        return "0,00 €"


def esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Stock", callback_data="v2menu:stock"),
         InlineKeyboardButton("🚨 Ruptures", callback_data="v2menu:ruptures")],
        [InlineKeyboardButton("📋 Commandes", callback_data="v2menu:commandes"),
         InlineKeyboardButton("🚚 Livraisons", callback_data="v2menu:livraisons")],
        [InlineKeyboardButton("🔧 Réparations", callback_data="v2menu:reparations"),
         InlineKeyboardButton("🔓 Déblocages", callback_data="v2menu:deblocages")],
        [InlineKeyboardButton("🏢 Fournisseurs", callback_data="v2menu:fournisseurs"),
         InlineKeyboardButton("📊 Statistiques", callback_data="stats")],
        [InlineKeyboardButton("📥📤 Mouvements", callback_data="v2menu:mouvements"),
         InlineKeyboardButton("🔎 Rechercher", callback_data="search")],
        [InlineKeyboardButton("📷 Scanner appareil", callback_data="scan_device")],
        [InlineKeyboardButton("👥 Collaborateurs", callback_data="v2menu:collaborateurs"),
         InlineKeyboardButton("📝 Activité", callback_data="activity")],
        [InlineKeyboardButton("🔄 Actualiser", callback_data="home")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Retour", callback_data="home")]
    ])


def _entry_datetime(item: dict[str, Any]) -> datetime | None:
    raw = item.get("date") or item.get("created_at") or item.get("updated_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Europe/Paris"))
    except (TypeError, ValueError):
        return None


def _is_finished(status: Any) -> bool:
    return str(status or "").strip().upper() in {
        "TERMINEE", "TERMINÉE", "LIVREE", "LIVRÉE", "PAYEE", "PAYÉE", "TERMINE", "TERMINÉ"
    }


def _amount(item: dict[str, Any]) -> float:
    for key in ("montant", "prix", "devis", "total", "prix_total"):
        try:
            return float(str(item.get(key, 0)).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            continue
    return 0.0


def dashboard_metrics() -> dict[str, Any]:
    now = datetime.now(ZoneInfo("Europe/Paris"))
    repairs = DB.get("reparations", [])
    unlocks = DB.get("deblocages", [])

    def same_day(x, y):
        d = _entry_datetime(x)
        return bool(d and d.date() == y.date())

    def same_month(x, y):
        d = _entry_datetime(x)
        return bool(d and d.year == y.year and d.month == y.month)

    repairs_day = [x for x in repairs if same_day(x, now)]
    repairs_month = [x for x in repairs if same_month(x, now)]
    unlock_day = [x for x in unlocks if same_day(x, now)]
    unlock_month = [x for x in unlocks if same_month(x, now)]
    frp_day = [x for x in unlock_day if "FRP" in str(x.get("type", "")).upper() or "GOOGLE" in str(x.get("type", "")).upper()]
    icloud_day = [x for x in unlock_day if "ICLOUD" in str(x.get("type", "")).upper() or "APPLE" in str(x.get("type", "")).upper()]
    frp_month = [x for x in unlock_month if "FRP" in str(x.get("type", "")).upper() or "GOOGLE" in str(x.get("type", "")).upper()]
    icloud_month = [x for x in unlock_month if "ICLOUD" in str(x.get("type", "")).upper() or "APPLE" in str(x.get("type", "")).upper()]

    # CA :
    # - réparations : uniquement les dossiers terminés/payés ;
    # - déblocages : chaque déblocage enregistré est comptabilisé,
    #   même s'il est encore "EN ATTENTE", car le prix est saisi lors
    #   de la création du dossier.
    revenue_repairs = [x for x in repairs if _is_finished(x.get("statut"))]
    revenue_unlocks = unlocks
    revenue_day = (
        sum(_amount(x) for x in revenue_repairs if same_day(x, now))
        + sum(_amount(x) for x in revenue_unlocks if same_day(x, now))
    )
    revenue_month = (
        sum(_amount(x) for x in revenue_repairs if same_month(x, now))
        + sum(_amount(x) for x in revenue_unlocks if same_month(x, now))
    )

    return {
        "repairs_day": len(repairs_day), "repairs_month": len(repairs_month),
        "unlock_day": len(unlock_day), "unlock_month": len(unlock_month),
        "frp_day": len(frp_day), "icloud_day": len(icloud_day),
        "frp_month": len(frp_month), "icloud_month": len(icloud_month),
        "revenue_day": revenue_day, "revenue_month": revenue_month,
    }


def home_text(current_chat_id: int | None = None) -> str:
    stock = DB["stock"]
    total_units = sum(int(x.get("quantite", 0)) for x in stock)
    value = sum(
        int(x.get("quantite", 0)) * float(x.get("prix_achat", 0))
        for x in stock
    )
    low = sum(
        1 for x in stock
        if 0 < int(x.get("quantite", 0))
        <= int(x.get("seuil", DB["settings"]["low_stock_default"]))
    )
    out = sum(1 for x in stock if int(x.get("quantite", 0)) <= 0)
    m = dashboard_metrics()

    return (
        "🔧 <b>ATELIERBOT — PRO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Stock : <b>{total_units}</b> unités\n"
        f"🟢 Références : <b>{len(stock)}</b>\n"
        f"🟠 Stock faible : <b>{low}</b>\n"
        f"🔴 Ruptures : <b>{out}</b>\n\n"
        f"📋 Commandes : <b>{len(DB['commandes'])}</b>\n"
        f"🚚 Livraisons : <b>{len(DB['livraisons'])}</b>\n"
        f"🔧 Réparations : <b>{len(DB['reparations'])}</b>\n"
        f"🏢 Fournisseurs : <b>{len(DB['fournisseurs'])}</b>\n\n"
        "📅 <b>ACTIVITÉ</b>\n"
        f"🔧 Réparations aujourd'hui : <b>{m['repairs_day']}</b>\n"
        f"🔧 Réparations ce mois : <b>{m['repairs_month']}</b>\n"
        f"🔓 Déblocages aujourd'hui : <b>{m['unlock_day']}</b>\n"
        f"🔓 Déblocages ce mois : <b>{m['unlock_month']}</b>\n"
        f"🔐 FRP / Google : <b>{m['frp_day']}</b> aujourd'hui • <b>{m['frp_month']}</b> ce mois\n"
        f"☁️ iCloud / Apple : <b>{m['icloud_day']}</b> aujourd'hui • <b>{m['icloud_month']}</b> ce mois\n"
        + (
            f"💶 CA aujourd'hui : <b>{money(m['revenue_day'])}</b>\n"
            f"💶 CA ce mois : <b>{money(m['revenue_month'])}</b>\n\n"
            f"💰 Valeur d'achat du stock : <b>{money(value)}</b>\n\n"
            if (admin(current_chat_id) or moderator(current_chat_id))
            else ""
        )
        + "🟢 <i>Base prête pour tes vraies données.</i>"
    )

async def require_access(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if authorized(chat_id):
        return True

    if update.callback_query:
        await update.callback_query.answer("🔒 Accès requis", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(
            "🔒 <b>Accès protégé</b>\n\n"
            "Utilise :\n<code>/access MOT_DE_PASSE</code>",
            parse_mode=ParseMode.HTML,
        )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update):
        return
    await update.effective_message.reply_text(
        home_text(update.effective_chat.id), parse_mode=ParseMode.HTML, reply_markup=menu()
    )


async def access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ATELIER_PASSWORD:
        await update.effective_message.reply_text(
            "⚠️ Le mot de passe collaborateur n'est pas configuré sur le serveur."
        )
        return

    chat_id = update.effective_chat.id
    if authorized(chat_id):
        await update.effective_message.reply_text("✅ Tu as déjà accès à AtelierBot.")
        return

    supplied = " ".join(context.args).strip()
    if supplied and supplied == ATELIER_PASSWORD:
        DB["users"][str(chat_id)] = {
            "role": "collaborateur",
            "name": update.effective_user.full_name if update.effective_user else "Collaborateur",
            "username": update.effective_user.username if update.effective_user else "",
            "added_at": now_iso(),
        }
        log_activity(chat_id, "ACCESS_GRANTED", "Nouveau collaborateur")
        await update.effective_message.reply_text(
            "🔓 <b>Accès autorisé !</b>\n\n"
            "Ton Chat ID est maintenant enregistré dans data.json.\n"
            "Tape /start.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.effective_message.reply_text(
        "❌ Mot de passe incorrect.\n\n"
        "Format : <code>/access MOT_DE_PASSE</code>",
        parse_mode=ParseMode.HTML,
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text(
        f"🆔 <b>Ton Chat ID</b>\n\n<code>{chat_id}</code>",
        parse_mode=ParseMode.HTML,
    )


def stock_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "📦 <b>STOCK</b>\n━━━━━━━━━━━━━━━━━━━━\n\nAucune référence."
    lines = ["📦 <b>STOCK</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for x in items[:40]:
        qty = int(x.get("quantite", 0))
        seuil = int(x.get("seuil", 2))
        icon = "🔴" if qty <= 0 else "🟠" if qty <= seuil else "🟢"
        lines.append(
            f"{icon} <b>{esc(x.get('produit'))}</b>\n"
            f"Réf. <code>{esc(x.get('reference'))}</code> • "
            f"{qty} unité(s)\n"
            f"📍 {esc(x.get('emplacement', '-'))} • "
            f"🏢 {esc(x.get('fournisseur', '-'))}\n"
            f"💶 Achat {money(x.get('prix_achat', 0))}"
        )
    if len(items) > 40:
        lines.append(f"\n… {len(items)-40} autres références.")
    return "\n\n".join(lines)


async def show_stock(update: Update):
    await update.effective_message.reply_text(
        stock_text(DB["stock"]), parse_mode=ParseMode.HTML, reply_markup=back_menu()
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await require_access(update):
        return

    action = q.data

    if action == "home":
        await q.edit_message_text(
            home_text(update.effective_chat.id), parse_mode=ParseMode.HTML, reply_markup=menu()
        )
        return

    if action == "stock":
        text = stock_text(DB["stock"])
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "ruptures":
        items = [
            x for x in DB["stock"]
            if int(x.get("quantite", 0)) <= int(x.get("seuil", 2))
        ]
        text = "🚨 <b>STOCK À SURVEILLER</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        text += stock_text(items)
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "commandes":
        lines = ["📋 <b>COMMANDES</b>", "━━━━━━━━━━━━━━━━━━━━"]
        if not DB["commandes"]:
            lines.append("\nAucune commande.")
        for x in DB["commandes"][:40]:
            lines.append(
                f"\n📦 <b>{esc(x.get('numero'))}</b>\n"
                f"🏢 {esc(x.get('fournisseur'))} • "
                f"📌 {esc(x.get('statut'))}\n"
                f"📅 {esc(x.get('date'))} • {money(x.get('montant', 0))}"
            )
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "livraisons":
        lines = ["🚚 <b>LIVRAISONS</b>", "━━━━━━━━━━━━━━━━━━━━"]
        if not DB["livraisons"]:
            lines.append("\nAucune livraison.")
        for x in DB["livraisons"][:40]:
            lines.append(
                f"\n📦 <b>{esc(x.get('commande'))}</b>\n"
                f"🚛 {esc(x.get('transporteur'))} • "
                f"📌 {esc(x.get('statut'))}\n"
                f"🔎 <code>{esc(x.get('suivi'))}</code>\n"
                f"📅 Prévue : {esc(x.get('date_prevue', '-'))}"
            )
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "reparations":
        lines = ["🔧 <b>RÉPARATIONS</b>", "━━━━━━━━━━━━━━━━━━━━"]
        if not DB["reparations"]:
            lines.append("\nAucune réparation.")
        for x in DB["reparations"][:40]:
            lines.append(
                f"\n🛠️ <b>{esc(x.get('numero'))}</b> • {esc(x.get('appareil'))}\n"
                f"👤 {esc(x.get('client'))}\n"
                f"🛠️ {esc(x.get('panne'))}\n"
                f"📌 {esc(x.get('statut'))} • 💶 {money(x.get('devis', 0))}"
            )
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "fournisseurs":
        lines = ["🏢 <b>FOURNISSEURS</b>", "━━━━━━━━━━━━━━━━━━━━"]
        if not DB["fournisseurs"]:
            lines.append("\nAucun fournisseur.")
        for x in DB["fournisseurs"][:40]:
            lines.append(
                f"\n🏢 <b>{esc(x.get('nom'))}</b>\n"
                f"👤 {esc(x.get('contact', '-'))}\n"
                f"📞 {esc(x.get('telephone', '-'))}\n"
                f"📧 {esc(x.get('email', '-'))}"
            )
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "stats":
        stock = DB["stock"]
        units = sum(int(x.get("quantite", 0)) for x in stock)
        value = sum(
            int(x.get("quantite", 0)) * float(x.get("prix_achat", 0))
            for x in stock
        )
        repair_open = sum(
            1 for x in DB["reparations"]
            if str(x.get("statut", "")).lower() not in {"terminée", "terminee", "livrée", "livree"}
        )
        delivered = sum(
            1 for x in DB["livraisons"]
            if str(x.get("statut", "")).lower() in {"livrée", "livree", "reçue", "recue"}
        )

        text = (
            "📊 <b>STATISTIQUES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Unités : <b>{units}</b>\n"
            f"🧩 Références : <b>{len(stock)}</b>\n"
            + (
                f"💰 Valeur achat : <b>{money(value)}</b>\n"
                if (admin(update.effective_chat.id) or moderator(update.effective_chat.id))
                else ""
            )
            + f"📋 Commandes : <b>{len(DB['commandes'])}</b>\n"
            + f"🚚 Livraisons reçues : <b>{delivered}</b>\n"
            + f"🔧 Réparations actives : <b>{repair_open}</b>\n"
            + f"🏢 Fournisseurs : <b>{len(DB['fournisseurs'])}</b>\n"
            + f"👥 Utilisateurs : <b>{len(DB['users'])}</b>\n"
            + f"📝 Événements journalisés : <b>{len(DB['activity_log'])}</b>"
        )
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "users":
        if not can_manage_users(update.effective_chat.id):
            await q.answer("🔒 Accès réservé à l’administrateur ou au modérateur.", show_alert=True)
            return

        lines = ["👥 <b>COLLABORATEURS</b>", "━━━━━━━━━━━━━━━━━━━━"]
        buttons = []

        for cid, rec in DB["users"].items():
            role = "👑 Admin" if rec.get("role") == "admin" else "👤 Collaborateur"
            lines.append(
                f"\n{role}\n"
                f"🆔 <code>{esc(cid)}</code>\n"
                f"Nom : {esc(rec.get('name', '-'))}"
            )

            # Seuls les collaborateurs peuvent être révoqués.
            if rec.get("role") != "admin":
                buttons.append([
                    InlineKeyboardButton(
                        f"🗑️ Révoquer — {str(rec.get('name', 'Collaborateur'))[:25]}",
                        callback_data=f"revoke:{cid}",
                    )
                ])

        buttons.append([
            InlineKeyboardButton("⬅️ Retour", callback_data="home")
        ])

        await q.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action.startswith("revoke:"):
        if not can_manage_users(update.effective_chat.id):
            await q.answer("🔒 Accès réservé à l’administrateur ou au modérateur.", show_alert=True)
            return

        target = action.split(":", 1)[1]
        rec = DB.get("users", {}).get(target)

        if not rec:
            await q.answer("Collaborateur introuvable.", show_alert=True)
            return

        if rec.get("role") == "admin" or target == str(ADMIN_CHAT_ID):
            await q.answer("Impossible de révoquer l'administrateur.", show_alert=True)
            return
        if moderator(update.effective_chat.id) and rec.get("role") == "moderateur":
            await q.answer("🔒 Un modérateur ne peut pas révoquer un autre modérateur.", show_alert=True)
            return

        name = rec.get("name", "Collaborateur")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Oui, révoquer",
                    callback_data=f"confirm_revoke:{target}",
                ),
                InlineKeyboardButton(
                    "❌ Annuler",
                    callback_data="users",
                ),
            ]
        ])

        await q.edit_message_text(
            "⚠️ <b>RÉVOQUER L'ACCÈS</b>\n\n"
            f"👤 {esc(name)}\n"
            f"🆔 <code>{esc(target)}</code>\n\n"
            "Cette personne ne pourra plus utiliser le bot tant que son accès "
            "n'aura pas été réautorisé.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    if action.startswith("confirm_revoke:"):
        if not can_manage_users(update.effective_chat.id):
            await q.answer("🔒 Accès réservé à l’administrateur ou au modérateur.", show_alert=True)
            return

        target = action.split(":", 1)[1]
        rec = DB.get("users", {}).get(target)

        if not rec:
            await q.answer("Déjà révoqué.", show_alert=True)
            return

        if rec.get("role") == "admin" or target == str(ADMIN_CHAT_ID):
            await q.answer("Impossible de révoquer l'administrateur.", show_alert=True)
            return
        if moderator(update.effective_chat.id) and rec.get("role") == "moderateur":
            await q.answer("🔒 Un modérateur ne peut pas révoquer un autre modérateur.", show_alert=True)
            return

        name = rec.get("name", "Collaborateur")
        DB["users"].pop(target, None)

        log_activity(
            update.effective_chat.id,
            "ACCESS_REVOKED",
            f"{name} ({target})",
        )

        await q.answer("🔒 Accès révoqué.")
        await q.edit_message_text(
            f"🔒 <b>Accès révoqué</b>\n\n"
            f"👤 {esc(name)}\n"
            f"🆔 <code>{esc(target)}</code>\n\n"
            "La personne n'a maintenant plus accès à AtelierBot.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Retour aux collaborateurs", callback_data="users")],
                [InlineKeyboardButton("⬅️ Accueil", callback_data="home")],
            ]),
        )
        return

    if action == "activity":
        if not admin(update.effective_chat.id):
            await q.answer("Administrateur uniquement", show_alert=True)
            return
        lines = ["📝 <b>ACTIVITÉ RÉCENTE</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for x in reversed(DB["activity_log"][-25:]):
            lines.append(
                f"\n🕒 {esc(x.get('date'))}\n"
                f"🆔 <code>{esc(x.get('chat_id'))}</code> • "
                f"<b>{esc(x.get('action'))}</b>\n"
                f"{esc(x.get('details'))}"
            )
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "search":
        context.user_data["awaiting_search"] = True
        await q.edit_message_text(
            "🔎 <b>RECHERCHE GLOBALE</b>\n\n"
            "Envoie une référence, un modèle, un IMEI, un numéro de commande, "
            "un suivi ou un numéro de réparation.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return

    if action == "scan_device":
        context.user_data.clear()
        context.user_data["scan_mode"] = True
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📷 Ouvrir la caméra", web_app=WebAppInfo(url=SCANNER_WEBAPP_URL))],
            [InlineKeyboardButton("⬅️ Retour", callback_data="home")],
        ])
        await q.edit_message_text(
            "📷 <b>SCANNER UN APPAREIL</b>\n\n"
            "Le bouton ci-dessous ouvre la <b>caméra de ton téléphone directement dans Telegram</b>.\n\n"
            "1️⃣ Choisis la référence stock dans le scanner.\n"
            "2️⃣ Cadre l'IMEI, le code-barres ou le QR code.\n"
            "3️⃣ Le résultat revient automatiquement dans le bot et ajoute l'appareil au stock.\n\n"
            "⚠️ La page doit être publiée en HTTPS (GitHub Pages convient).",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    if action == "add_stock":
        context.user_data.clear()
        context.user_data["step"] = "stock_produit"
        await q.edit_message_text(
            "➕ <b>AJOUT STOCK</b>\n\nEnvoie le nom du produit/modèle.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return

    if action == "movement":
        await q.edit_message_text(
            "📥📤 <b>MOUVEMENT DE STOCK</b>\n\n"
            "Utilise les commandes rapides :\n"
            "<code>/in REF QUANTITE</code>\n"
            "<code>/out REF QUANTITE</code>\n\n"
            "Exemple : <code>/in IP15PRO-BLK-256 3</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return


async def search_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return
    if context.user_data.get("scan_mode"):
        await scanner_message(update, context)
        return
    if not context.user_data.get("awaiting_search"):
        return

    query = update.effective_message.text.strip().lower()
    context.user_data["awaiting_search"] = False

    results: list[str] = []

    for x in DB["appareils"]:
        blob = " ".join(str(v) for v in x.values()).lower()
        if query in blob:
            results.append(
                f"📱 <b>APPAREIL</b>\n"
                f"{esc(x.get('type_identifiant'))} : <code>{esc(x.get('identifiant'))}</code>\n"
                f"Réf. : <code>{esc(x.get('reference'))}</code> • "
                f"Statut : {esc(x.get('statut'))}\n"
                f"Entrée : {esc(x.get('date_entree'))}"
            )

    for x in DB["stock"]:
        blob = " ".join(str(v) for v in x.values()).lower()
        if query in blob:
            results.append(
                f"📦 <b>STOCK</b>\n"
                f"{esc(x.get('produit'))} • "
                f"<code>{esc(x.get('reference'))}</code>\n"
                f"Quantité : {x.get('quantite', 0)} • "
                f"Emplacement : {esc(x.get('emplacement', '-'))}"
            )

    for x in DB["commandes"]:
        blob = " ".join(str(v) for v in x.values()).lower()
        if query in blob:
            results.append(
                f"📋 <b>COMMANDE</b>\n"
                f"<code>{esc(x.get('numero'))}</code> • "
                f"{esc(x.get('fournisseur'))}\n"
                f"Statut : {esc(x.get('statut'))}"
            )

    for x in DB["livraisons"]:
        blob = " ".join(str(v) for v in x.values()).lower()
        if query in blob:
            results.append(
                f"🚚 <b>LIVRAISON</b>\n"
                f"Suivi : <code>{esc(x.get('suivi'))}</code>\n"
                f"{esc(x.get('transporteur'))} • {esc(x.get('statut'))}"
            )

    for x in DB["reparations"]:
        blob = " ".join(str(v) for v in x.values()).lower()
        if query in blob:
            results.append(
                f"🔧 <b>RÉPARATION</b>\n"
                f"<code>{esc(x.get('numero'))}</code> • "
                f"{esc(x.get('appareil'))}\n"
                f"{esc(x.get('client'))} • {esc(x.get('statut'))}"
            )

    if not results:
        text = f"🔎 Aucun résultat pour : <code>{esc(query)}</code>"
    else:
        text = f"🔎 <b>{len(results)} résultat(s)</b>\n\n" + "\n\n".join(results[:30])

    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=menu()
    )


async def add_stock_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return ConversationHandler.END

    text = update.effective_message.text.strip()
    step = context.user_data.get("step")

    prompts = {
        "stock_produit": ("produit", "Envoie la référence interne."),
        "stock_reference": ("reference", "Envoie la quantité."),
        "stock_quantite": ("quantite", "Envoie le prix d'achat unitaire, ex. 120.50"),
        "stock_prix": ("prix_achat", "Envoie le seuil d'alerte, ex. 2"),
        "stock_seuil": ("seuil", "Envoie l'emplacement, ex. Tiroir A3"),
        "stock_emplacement": ("emplacement", "Envoie le fournisseur."),
    }

    if step in prompts:
        field, prompt = prompts[step]
        context.user_data[field] = text

        next_step = {
            "stock_produit": "stock_reference",
            "stock_reference": "stock_quantite",
            "stock_quantite": "stock_prix",
            "stock_prix": "stock_seuil",
            "stock_seuil": "stock_emplacement",
            "stock_emplacement": "stock_fournisseur",
        }[step]
        context.user_data["step"] = next_step

        if next_step == "stock_fournisseur":
            await update.effective_message.reply_text("🏢 Envoie le fournisseur.")
        else:
            await update.effective_message.reply_text(prompt)
        return ADD_STOCK

    if step == "stock_fournisseur":
        context.user_data["fournisseur"] = text
        try:
            qty = int(context.user_data["quantite"])
            price = float(context.user_data["prix_achat"].replace(",", "."))
            threshold = int(context.user_data["seuil"])
        except ValueError:
            await update.effective_message.reply_text(
                "❌ Valeur invalide. Recommence avec /start."
            )
            return ConversationHandler.END

        item = {
            "id": f"STK-{len(DB['stock'])+1:05d}",
            "produit": context.user_data["produit"],
            "reference": context.user_data["reference"],
            "quantite": qty,
            "prix_achat": price,
            "seuil": threshold,
            "emplacement": context.user_data["emplacement"],
            "fournisseur": text,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        DB["stock"].append(item)
        DB["mouvements"].append({
            "date": now_iso(),
            "reference": item["reference"],
            "type": "ENTREE_INITIALE",
            "quantite": qty,
            "chat_id": update.effective_chat.id,
        })
        log_activity(update.effective_chat.id, "STOCK_CREATE", item["reference"])

        await update.effective_message.reply_text(
            "✅ <b>Référence ajoutée</b>\n\n"
            f"📦 {esc(item['produit'])}\n"
            f"🔖 <code>{esc(item['reference'])}</code>\n"
            f"📊 {qty} unité(s)\n"
            f"💶 {money(price)}\n"
            f"📍 {esc(item['emplacement'])}",
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    return ConversationHandler.END


async def add_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return ConversationHandler.END

    text = update.effective_message.text.strip()
    step = context.user_data.get("step")

    if step == "supplier_nom":
        context.user_data["nom"] = text
        context.user_data["step"] = "supplier_contact"
        await update.effective_message.reply_text("👤 Contact.")
        return ADD_SUPPLIER
    if step == "supplier_contact":
        context.user_data["contact"] = text
        context.user_data["step"] = "supplier_phone"
        await update.effective_message.reply_text("📞 Téléphone.")
        return ADD_SUPPLIER
    if step == "supplier_phone":
        context.user_data["telephone"] = text
        context.user_data["step"] = "supplier_email"
        await update.effective_message.reply_text("📧 E-mail (ou -).")
        return ADD_SUPPLIER
    if step == "supplier_email":
        DB["fournisseurs"].append({
            "id": f"SUP-{len(DB['fournisseurs'])+1:05d}",
            "nom": context.user_data["nom"],
            "contact": context.user_data["contact"],
            "telephone": context.user_data["telephone"],
            "email": text,
            "created_at": now_iso(),
        })
        log_activity(update.effective_chat.id, "SUPPLIER_CREATE", context.user_data["nom"])
        context.user_data.clear()
        await update.effective_message.reply_text(
            "✅ Fournisseur ajouté.", reply_markup=menu()
        )
        return ConversationHandler.END
    return ConversationHandler.END


async def quick_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stock_movement(update, context, "IN")


async def quick_out(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stock_movement(update, context, "OUT")


async def stock_movement(update: Update, context: ContextTypes.DEFAULT_TYPE, direction: str):
    if not await require_access(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            f"Format : /{'in' if direction == 'IN' else 'out'} REF QUANTITE"
        )
        return

    ref = context.args[0]
    try:
        qty = int(context.args[1])
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("❌ Quantité invalide.")
        return

    item = next((x for x in DB["stock"] if x.get("reference") == ref), None)
    if not item:
        await update.effective_message.reply_text("❌ Référence introuvable.")
        return

    old = int(item.get("quantite", 0))
    new = old + qty if direction == "IN" else old - qty
    if new < 0:
        await update.effective_message.reply_text(
            f"❌ Stock insuffisant. Disponible : {old}."
        )
        return

    item["quantite"] = new
    item["updated_at"] = now_iso()
    DB["mouvements"].append({
        "date": now_iso(),
        "reference": ref,
        "type": direction,
        "quantite": qty,
        "avant": old,
        "apres": new,
        "chat_id": update.effective_chat.id,
    })
    log_activity(
        update.effective_chat.id,
        f"STOCK_{direction}",
        f"{ref}: {old} -> {new}",
    )
    await update.effective_message.reply_text(
        f"✅ Stock {ref} : <b>{old}</b> → <b>{new}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
    )


def get_barcode_map() -> dict:
    """Retourne la table persistante code-barres/QR -> référence stock."""
    mapping = DB.setdefault("codes_stock", {})
    if not isinstance(mapping, dict):
        mapping = {}
        DB["codes_stock"] = mapping
    return mapping


def remember_stock_code(value: str, ref: str, kind: str) -> None:
    """Mémorise un code pour qu'il puisse être reconnu lors des prochains scans."""
    mapping = get_barcode_map()
    mapping[value] = {
        "reference": ref,
        "type": kind,
        "updated_at": now_iso(),
    }
    save_db(DB, make_backup=False)


def find_reference_from_code(value: str):
    """Retrouve la référence stock associée à un code mémorisé."""
    entry = get_barcode_map().get(value)
    if isinstance(entry, dict):
        ref = str(entry.get("reference", "")).strip()
        if ref:
            return ref
    return None


def valid_imei(value: str) -> bool:
    if len(value) != 15 or not value.isdigit():
        return False
    total = 0
    for i, ch in enumerate(value):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def valid_barcode(value: str) -> bool:
    return 4 <= len(value) <= 40 and value.isalnum()


async def start_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update):
        return
    context.user_data.clear()
    context.user_data["scan_mode"] = True
    context.user_data["scan_step"] = "reference"
    await update.effective_message.reply_text(
        "📷 <b>SCAN APPAREIL</b>\n\n"
        "1️⃣ Envoie la <b>référence stock</b> à utiliser.\n"
        "2️⃣ Branche ton scanner USB/Bluetooth et scanne l’IMEI ou le code-barres.\n"
        "3️⃣ Chaque scan valide ajoute automatiquement <b>1 appareil</b> au stock.\n\n"
        "Tu peux scanner plusieurs appareils à la suite.\n"
        "Tape /stopscan pour terminer.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu(),
    )


async def scanner_webapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return
    msg = update.effective_message
    if not msg or not msg.web_app_data:
        return

    try:
        payload = json.loads(msg.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        await msg.reply_text("❌ Données de scan invalides.")
        return

    ref = str(payload.get("reference", "")).strip()
    value = str(payload.get("value", "")).strip().replace(" ", "")
    kind_hint = str(payload.get("type", "")).upper()

    # Détermine le type du code.
    is_imei = value.isdigit() and len(value) == 15
    if is_imei:
        if not valid_imei(value):
            await msg.reply_text("❌ IMEI invalide (contrôle Luhn échoué).")
            return
        kind = "IMEI"
    elif valid_barcode(value) or kind_hint in {"QR", "BARCODE", "CODE_BARRES"}:
        kind = "QR" if kind_hint == "QR" else "CODE_BARRES"
    else:
        await msg.reply_text("❌ Code non reconnu.")
        return

    # Si aucune référence n'est fournie, on tente la reconnaissance automatique.
    if not ref:
        ref = find_reference_from_code(value)
        if not ref:
            await msg.reply_text(
                "❌ Code inconnu.\n\n"
                "Pour l'associer, relance le scanner en indiquant d'abord la référence stock, "
                "puis scanne ce code."
            )
            return
        await msg.reply_text(
            f"🔎 Code reconnu automatiquement.\n"
            f"📦 Réf. <code>{esc(ref)}</code>",
            parse_mode=ParseMode.HTML,
        )

    item = next((x for x in DB["stock"] if str(x.get("reference")) == ref), None)
    if not item:
        await msg.reply_text(
            f"❌ Référence <code>{esc(ref)}</code> introuvable. Ajoute-la d'abord dans le stock.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Pour les codes-barres/QR, mémorise l'association référence <-> code.
    # Un IMEI reste un identifiant unique d'appareil et n'est pas utilisé
    # comme modèle permanent de référence.
    if kind in {"QR", "CODE_BARRES"}:
        existing = find_reference_from_code(value)
        if existing and existing != ref:
            await msg.reply_text(
                f"⚠️ Ce code est déjà associé à <code>{esc(existing)}</code>.\n"
                f"Il n'a pas été réassigné à <code>{esc(ref)}</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        remember_stock_code(value, ref, kind)

    if any(str(x.get("identifiant")) == value for x in DB["appareils"]):
        await msg.reply_text("⚠️ Cet identifiant est déjà enregistré dans le stock.")
        return

    old = int(item.get("quantite", 0))
    item["quantite"] = old + 1
    item["updated_at"] = now_iso()
    DB["appareils"].append({
        "id": f"DEV-{len(DB['appareils'])+1:06d}",
        "reference": ref,
        "identifiant": value,
        "type_identifiant": kind,
        "date_entree": now_iso(),
        "chat_id": update.effective_chat.id,
        "statut": "EN_STOCK",
    })
    DB["mouvements"].append({
        "date": now_iso(),
        "reference": ref,
        "type": "IN_SCAN",
        "quantite": 1,
        "avant": old,
        "apres": old + 1,
        "identifiant": value,
        "chat_id": update.effective_chat.id,
    })
    log_activity(update.effective_chat.id, "STOCK_IN_SCAN", f"{ref}: {value}")

    await msg.reply_text(
        f"✅ <b>{kind} enregistré</b>\n"
        f"🔖 <code>{esc(value)}</code>\n"
        f"📦 Réf. <code>{esc(ref)}</code>\n"
        f"📊 Stock : <b>{old} → {old + 1}</b>\n\n"
        "📷 Prêt pour le scan suivant.",
        parse_mode=ParseMode.HTML,
    )


async def scanner_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return
    if not context.user_data.get("scan_mode"):
        return

    text = update.effective_message.text.strip()
    step = context.user_data.get("scan_step")

    if step == "reference":
        item = next((x for x in DB["stock"] if str(x.get("reference")) == text), None)
        if not item:
            await update.effective_message.reply_text(
                "❌ Référence introuvable. Ajoute d’abord la référence dans le stock, puis renvoie sa référence."
            )
            return
        context.user_data["scan_reference"] = text
        context.user_data["scan_step"] = "scan"
        await update.effective_message.reply_text(
            f"✅ Référence <code>{esc(text)}</code> sélectionnée.\n\n"
            "📷 Tu peux maintenant scanner les IMEI/codes-barres un par un.\n"
            "Tape /stopscan quand tu as terminé.",
            parse_mode=ParseMode.HTML,
        )
        return

    value = text.replace(" ", "")
    # Si un code-barres/QR a déjà été associé, on peut retrouver automatiquement la référence.
    known_ref = find_reference_from_code(value)
    if known_ref:
        context.user_data["scan_reference"] = known_ref
        ref_item = next((x for x in DB["stock"] if str(x.get("reference")) == known_ref), None)
        if ref_item:
            await update.effective_message.reply_text(
                f"🔎 Code reconnu : <code>{esc(value)}</code>\n"
                f"📦 Réf. <code>{esc(known_ref)}</code>\n"
                "➕ Ajout automatique de 1 unité.",
                parse_mode=ParseMode.HTML,
            )
        # Continue normalement avec cette référence.
    is_imei = value.isdigit() and len(value) == 15
    if is_imei:
        if not valid_imei(value):
            await update.effective_message.reply_text("❌ IMEI invalide (contrôle Luhn échoué).")
            return
        kind = "IMEI"
    elif valid_barcode(value):
        kind = "CODE_BARRES"
    else:
        await update.effective_message.reply_text(
            "❌ Scan non reconnu. Scanne un IMEI de 15 chiffres ou un code-barres alphanumérique."
        )
        return

    ref = context.user_data["scan_reference"]
    item = next((x for x in DB["stock"] if str(x.get("reference")) == ref), None)
    if not item:
        await update.effective_message.reply_text("❌ La référence sélectionnée n’existe plus.")
        context.user_data.clear()
        return

    if kind == "CODE_BARRES":
        existing = find_reference_from_code(value)
        if existing and existing != ref:
            await update.effective_message.reply_text(
                f"⚠️ Ce code est déjà associé à <code>{esc(existing)}</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        remember_stock_code(value, ref, kind)

    if any(str(x.get("identifiant")) == value for x in DB["appareils"]):
        await update.effective_message.reply_text("⚠️ Cet appareil est déjà enregistré.")
        return

    old = int(item.get("quantite", 0))
    item["quantite"] = old + 1
    item["updated_at"] = now_iso()
    DB["appareils"].append({
        "id": f"DEV-{len(DB['appareils'])+1:06d}",
        "reference": ref,
        "identifiant": value,
        "type_identifiant": kind,
        "date_entree": now_iso(),
        "chat_id": update.effective_chat.id,
        "statut": "EN_STOCK",
    })
    DB["mouvements"].append({
        "date": now_iso(),
        "reference": ref,
        "type": "IN_SCAN",
        "quantite": 1,
        "avant": old,
        "apres": old + 1,
        "identifiant": value,
        "chat_id": update.effective_chat.id,
    })
    log_activity(update.effective_chat.id, "STOCK_IN_SCAN", f"{ref}: {value}")

    await update.effective_message.reply_text(
        f"✅ <b>{kind} enregistré</b>\n"
        f"🔖 <code>{esc(value)}</code>\n"
        f"📦 Réf. <code>{esc(ref)}</code>\n"
        f"📊 Stock : <b>{old} → {old + 1}</b>\n\n"
        "📷 Prêt pour le scan suivant.",
        parse_mode=ParseMode.HTML,
    )


async def stop_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("scan_mode"):
        await update.effective_message.reply_text("ℹ️ Aucun scan en cours.")
        return
    context.user_data.clear()
    await update.effective_message.reply_text(
        "🛑 <b>Scan terminé.</b>", parse_mode=ParseMode.HTML, reply_markup=menu()
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("❌ Opération annulée.", reply_markup=menu())
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🛠️ <b>ATELIERBOT</b>\n\n"
        "/start — tableau de bord\n"
        "/myid — ton Chat ID\n"
        "/access MOT_DE_PASSE — accès collaborateur\n"
        "/in REF QTE — entrée stock\n"
        "/out REF QTE — sortie stock\n"
        "/scan — scanner IMEI/code-barres\n"
        "/stopscan — arrêter le scan\n"
        "/cancel — annuler\n"
        "/help — aide",
        parse_mode=ParseMode.HTML,
    )



# ============================================================
# V2/V3 — SOUS-MENUS + FORMULAIRES COMPLETS
# ============================================================

V2_SECTIONS = {
 "stock": ("📦 STOCK", [("➕ Ajouter", "v2act:stock:add"), ("📋 Voir", "v2act:stock:list"), ("🔎 Rechercher", "v2act:stock:search"), ("✏️ Modifier", "v2act:stock:edit"), ("🗑️ Supprimer", "v2act:stock:delete"), ("📋 Inventaire", "v2act:stock:inventory")]),
 "ruptures": ("🚨 RUPTURES", [("➕ Ajouter", "v2act:ruptures:add"), ("📋 Voir", "v2act:ruptures:list"), ("🔎 Rechercher", "v2act:ruptures:search")]),
 "commandes": ("📋 COMMANDES", [("➕ Ajouter", "v2act:commandes:add"), ("📋 Voir", "v2act:commandes:list"), ("🔎 Rechercher", "v2act:commandes:search"), ("✏️ Modifier", "v2act:commandes:edit"), ("🗑️ Supprimer", "v2act:commandes:delete")]),
 "livraisons": ("🚚 LIVRAISONS", [("➕ Ajouter", "v2act:livraisons:add"), ("📋 Voir", "v2act:livraisons:list"), ("🔎 Rechercher", "v2act:livraisons:search"), ("✏️ Modifier", "v2act:livraisons:edit"), ("🗑️ Supprimer", "v2act:livraisons:delete")]),
 "reparations": ("🔧 RÉPARATIONS", [("➕ Ajouter une réparation", "v2act:reparations:add"), ("📋 Voir", "v2act:reparations:list"), ("🔎 Rechercher", "v2act:reparations:search"), ("✏️ Modifier", "v2act:reparations:edit"), ("🗑️ Supprimer", "v2act:reparations:delete"), ("⏸️ Pause", "v2act:reparations:pause"), ("▶️ Reprendre", "v2act:reparations:resume"), ("📦 Attente pièce", "v2act:reparations:parts"), ("👤 Attente client", "v2act:reparations:customer"), ("🧪 À tester", "v2act:reparations:test"), ("✅ Terminer", "v2act:reparations:done"), ("📦 Livrée", "v2act:reparations:delivered"), ("❌ Annuler", "v2act:reparations:cancel")]),
 "deblocages": ("🔓 DÉBLOCAGES", [("➕ Nouveau dossier", "v2act:deblocages:add"), ("📋 Voir", "v2act:deblocages:list"), ("🔎 Rechercher", "v2act:deblocages:search")]),
 "fournisseurs": ("🏢 FOURNISSEURS", [("➕ Ajouter", "v2act:fournisseurs:add"), ("📋 Voir", "v2act:fournisseurs:list"), ("🔎 Rechercher", "v2act:fournisseurs:search"), ("✏️ Modifier", "v2act:fournisseurs:edit"), ("🗑️ Supprimer", "v2act:fournisseurs:delete")]),
 "mouvements": ("📥📤 MOUVEMENTS", [("📥 Entrée", "v2act:mouvements:in"), ("📤 Sortie", "v2act:mouvements:out"), ("📋 Historique", "v2act:mouvements:list")]),
 "collaborateurs": ("👥 COLLABORATEURS", [("➕ Ajouter", "v2act:collaborateurs:add"), ("📋 Voir", "v2act:collaborateurs:list"), ("🗑️ Révoquer", "v2act:collaborateurs:delete"), ("🛡️ Gérer les rôles", "v2act:collaborateurs:roles")]),
}
V2_STATUSES = ["EN ATTENTE", "EN COURS", "EN PAUSE", "EN ATTENTE PIÈCE", "EN ATTENTE CLIENT", "À TESTER", "TERMINÉE", "LIVRÉE", "ANNULÉE"]


def v2_keyboard(section):
    return InlineKeyboardMarkup([[InlineKeyboardButton(a, callback_data=d)] for a, d in V2_SECTIONS[section][1]] + [[InlineKeyboardButton("⬅️ Retour", callback_data="home")]])


def _user_rows(include_admin=False):
    rows = []
    for cid, rec in DB.get("users", {}).items():
        rec = rec if isinstance(rec, dict) else {}
        if include_admin or str(rec.get("role", "")) != "admin":
            rows.append((str(cid), rec))
    return rows


def v2_text(section, qry=None, current_chat_id=None):
    if section == "collaborateurs":
        rows = _user_rows()
        if qry:
            q = str(qry).lower()
            rows = [(cid, rec) for cid, rec in rows if q in cid.lower() or q in " ".join(str(v) for v in rec.values()).lower()]
        title = V2_SECTIONS[section][0]
        if not rows:
            return title + "\n━━━━━━━━━━━━━━━━━━━━\n\nAucun collaborateur enregistré."
        lines = [title, "━━━━━━━━━━━━━━━━━━━━"]
        for cid, rec in rows[:40]:
            name = rec.get("name") or "Collaborateur"
            username = rec.get("username")
            role = str(rec.get("role", "collaborateur"))
            role_label = "🛡️ Modérateur" if role == "moderateur" else "👤 Collaborateur"
            # Le Chat ID est visible par l'admin et les modérateurs.
            if admin(current_chat_id) or moderator(current_chat_id):
                lines.append(f"{role_label} — <b>{esc(name)}</b>\n🆔 <code>{esc(cid)}</code>" + (f"\n📱 @{esc(username)}" if username else ""))
            else:
                lines.append(f"{role_label} — <b>{esc(name)}</b>" + (f"\n📱 @{esc(username)}" if username else ""))
        return "\n\n".join(lines)

    items = DB.get(section, [])
    if section == "ruptures":
        items = [x for x in DB.get("stock", []) if int(x.get("quantite", 0)) <= int(x.get("seuil", 2))]
    if qry:
        q = str(qry).lower()
        items = [x for x in items if q in " ".join(str(v) for v in x.values()).lower()]
    title = V2_SECTIONS[section][0]
    if not items:
        return title + "\n━━━━━━━━━━━━━━━━━━━━\n\nAucun élément."
    lines = [title, "━━━━━━━━━━━━━━━━━━━━"]
    for x in items[:40]:
        lines.append("\n" + esc(str(x)[:900]))
    return "\n".join(lines)


def _start_flow(context, flow, steps):
    context.user_data.clear()
    context.user_data["v2_flow"] = flow
    context.user_data["v2_step"] = 1
    context.user_data["v2_steps"] = steps
    context.user_data["v2_form"] = {}


async def v2_handler(update, context):
    q = update.callback_query
    if not q or not await require_access(update):
        return
    await q.answer()
    data = q.data or ""
    if data.startswith("v2menu:"):
        sec = data.split(":", 1)[1]
        if sec in V2_SECTIONS:
            await q.edit_message_text(V2_SECTIONS[sec][0] + "\n\nChoisis une action :", reply_markup=v2_keyboard(sec))
        return
    if not data.startswith("v2act:"):
        return
    _, sec, act = data.split(":", 2)
    if sec not in V2_SECTIONS:
        return

    if sec == "collaborateurs" and act in {"add", "delete"} and not can_manage_users(update.effective_chat.id):
        await q.answer("🔒 Réservé à l’administrateur ou au modérateur.", show_alert=True)
        return

    if sec == "collaborateurs" and act == "roles" and not admin(update.effective_chat.id):
        await q.answer("🔒 Seul l'administrateur peut gérer les rôles.", show_alert=True)
        return

    if sec == "collaborateurs" and act == "add":
        _start_flow(context, "collaborateur_add", [("chat_id", "Envoie son Chat ID Telegram.")])
        await q.edit_message_text("👥 <b>AJOUTER UN COLLABORATEUR</b>\n\nEnvoie son <b>Chat ID Telegram</b>.", parse_mode=ParseMode.HTML, reply_markup=back_menu()); return
    if sec == "collaborateurs" and act == "delete":
        _start_flow(context, "collaborateur_revoke", [("needle", "Envoie son Chat ID, son nom ou son @username.")])
        await q.edit_message_text("🗑️ <b>RÉVOQUER UN COLLABORATEUR</b>\n\nEnvoie son <b>Chat ID</b>, son nom ou son @username.", parse_mode=ParseMode.HTML, reply_markup=back_menu()); return

    if sec == "collaborateurs" and act == "roles":
        _start_flow(context, "collaborateur_role", [("needle", "Envoie son Chat ID, son nom ou son @username.")])
        await q.edit_message_text(
            "🛡️ <b>GESTION DES RÔLES</b>\n\n"
            "Envoie le <b>Chat ID</b>, le nom ou le <b>@username</b> de la personne à modifier.",
            parse_mode=ParseMode.HTML, reply_markup=back_menu()
        ); return

    if sec == "stock" and act == "add":
        _start_flow(context, "stock_add_v2", [("produit", "1/7 — Nom du produit/modèle ?"), ("reference", "2/7 — Référence interne ?"), ("quantite", "3/7 — Quantité ?"), ("prix_achat", "4/7 — Prix d'achat unitaire ?"), ("seuil", "5/7 — Seuil d'alerte ?"), ("emplacement", "6/7 — Emplacement ?"), ("fournisseur", "7/7 — Fournisseur ?")])
        await q.edit_message_text("➕ <b>AJOUT STOCK</b>\n\n1/7 — Nom du produit/modèle ?", parse_mode=ParseMode.HTML, reply_markup=back_menu()); return

    if sec == "stock" and act == "inventory":
        items = DB.get("stock", [])
        if not items:
            await q.edit_message_text("📋 <b>INVENTAIRE</b>\n\nAucune référence dans le stock.", parse_mode=ParseMode.HTML, reply_markup=back_menu())
            return
        context.user_data.clear()
        context.user_data["inventory_items"] = [str(x.get("reference", "")) for x in items]
        context.user_data["inventory_index"] = 0
        context.user_data["inventory_corrections"] = []
        ref = context.user_data["inventory_items"][0]
        item = next((x for x in items if str(x.get("reference", "")) == ref), None)
        theoretical = int(item.get("quantite", 0)) if item else 0
        await q.edit_message_text(
            f"📋 <b>INVENTAIRE</b>\n\n1/{len(items)} — <b>{esc(item.get('produit', ref) if item else ref)}</b>\n"
            f"🔖 Réf. : <code>{esc(ref)}</code>\n"
            f"📦 Stock théorique : <b>{theoretical}</b>\n\n"
            "Envoie la quantité réellement comptée.",
            parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if sec == "ruptures" and act == "add":
        _start_flow(context, "rupture_add", [("reference", "Envoie la référence du produit à mettre en rupture.")])
        await q.edit_message_text(
            "➕ <b>AJOUTER UNE RUPTURE</b>\n\n"
            "Envoie la <b>référence exacte</b> du produit existant.\n"
            "Le stock sera ramené à <b>0</b> et le mouvement sera enregistré.",
            parse_mode=ParseMode.HTML, reply_markup=back_menu()
        ); return

    flow_specs = {
        ("commandes", "add"): ("commande_add", [("numero", "1/4 — Numéro de commande ?"), ("fournisseur", "2/4 — Fournisseur ?"), ("montant", "3/4 — Montant ?"), ("statut", "4/4 — Statut ?")], "📋 <b>NOUVELLE COMMANDE</b>\n\n1/4 — Numéro de commande ?"),
        ("livraisons", "add"): ("livraison_add", [("commande", "1/5 — Numéro de commande ?"), ("transporteur", "2/5 — Transporteur ?"), ("suivi", "3/5 — Numéro de suivi ?"), ("date_prevue", "4/5 — Date prévue ?"), ("statut", "5/5 — Statut ?")], "🚚 <b>NOUVELLE LIVRAISON</b>\n\n1/5 — Numéro de commande ?"),
        ("fournisseurs", "add"): ("fournisseur_add_v2", [("nom", "1/4 — Nom du fournisseur ?"), ("contact", "2/4 — Contact ?"), ("telephone", "3/4 — Téléphone ?"), ("email", "4/4 — E-mail (ou -) ?")], "🏢 <b>NOUVEAU FOURNISSEUR</b>\n\n1/4 — Nom du fournisseur ?"),
        ("mouvements", "in"): ("movement_in", [("reference", "1/2 — Référence stock ?"), ("quantite", "2/2 — Quantité à entrer ?")], "📥 <b>ENTRÉE STOCK</b>\n\n1/2 — Référence stock ?"),
        ("mouvements", "out"): ("movement_out", [("reference", "1/2 — Référence stock ?"), ("quantite", "2/2 — Quantité à sortir ?")], "📤 <b>SORTIE STOCK</b>\n\n1/2 — Référence stock ?"),
        ("reparations", "add"): ("reparation", [("numero", "1/7 — Numéro de réparation ?"), ("appareil", "2/7 — Modèle/appareil ?"), ("client", "3/7 — Client ?"), ("panne", "4/7 — Panne ?"), ("imei", "5/7 — IMEI (ou -) ?"), ("devis", "6/7 — Prix/devis ?"), ("statut", "7/7 — Statut ?")], "🔧 <b>NOUVELLE RÉPARATION</b>\n\n1/7 — Numéro de réparation ?"),
        ("deblocages", "add"): ("deblocage", [("type", "1/5 — FRP / Google ou iCloud / Apple ?"), ("appareil", "2/5 — Modèle/appareil ?"), ("imei", "3/5 — IMEI (ou -) ?"), ("client", "4/5 — Client ?"), ("montant", "5/5 — Prix du déblocage ?")], "🔓 <b>NOUVEAU DOSSIER</b>\n\n1/5 — FRP / Google ou iCloud / Apple ?"),
    }
    spec = flow_specs.get((sec, act))
    if spec:
        flow, steps, prompt = spec
        _start_flow(context, flow, steps)
        await q.edit_message_text(prompt, parse_mode=ParseMode.HTML, reply_markup=back_menu()); return

    if act == "list":
        await q.edit_message_text(v2_text(sec, current_chat_id=update.effective_chat.id), reply_markup=v2_keyboard(sec), parse_mode=ParseMode.HTML); return
    if act == "search":
        context.user_data["v2_search"] = sec
        await q.edit_message_text("🔎 Envoie le terme à rechercher.", reply_markup=back_menu()); return
    if sec == "mouvements" and act == "list":
        await q.edit_message_text(v2_text("mouvements", current_chat_id=update.effective_chat.id), reply_markup=v2_keyboard("mouvements"), parse_mode=ParseMode.HTML); return
    if sec == "reparations" and act in {"pause", "resume", "parts", "customer", "test", "done", "delivered", "cancel"}:
        st = {"pause":"EN PAUSE","resume":"EN COURS","parts":"EN ATTENTE PIÈCE","customer":"EN ATTENTE CLIENT","test":"À TESTER","done":"TERMINÉE","delivered":"LIVRÉE","cancel":"ANNULÉE"}[act]
        context.user_data["v2_status"] = st
        await q.edit_message_text(f"📌 Nouveau statut : <b>{st}</b>\n\nEnvoie le numéro ou l'IMEI de la réparation.", parse_mode=ParseMode.HTML, reply_markup=back_menu()); return
    if act in {"edit", "delete"}:
        context.user_data["v2_search_action"] = (sec, act)
        await q.edit_message_text("🔎 Envoie le numéro, l'IMEI ou un élément permettant d'identifier l'entrée.", reply_markup=back_menu()); return
    await q.edit_message_text("ℹ️ Action non disponible.", reply_markup=v2_keyboard(sec))


def _persist_and_clear(context):
    save_db(DB)
    context.user_data.clear()


def _safe_float(value):
    return float(str(value).replace(" ", "").replace(",", "."))


def _safe_int(value):
    return int(str(value).strip())


async def _finish_flow(update, context, flow, f):
    cid = update.effective_chat.id
    if flow == "stock_add_v2":
        try:
            f["quantite"] = _safe_int(f["quantite"]); f["prix_achat"] = _safe_float(f["prix_achat"]); f["seuil"] = _safe_int(f["seuil"])
        except ValueError:
            await update.effective_message.reply_text("❌ Quantité, prix ou seuil invalide. Recommence l'ajout.", reply_markup=back_menu()); return True
        item = {"id": f"STK-{len(DB['stock'])+1:05d}", **f, "created_at": now_iso(), "updated_at": now_iso()}
        DB["stock"].append(item)
        DB["mouvements"].append({"date": now_iso(), "reference": item["reference"], "type": "ENTREE_INITIALE", "quantite": item["quantite"], "chat_id": cid})
        log_activity(cid, "STOCK_CREATE", item["reference"])
        context.user_data.clear(); await update.effective_message.reply_text("✅ <b>Référence ajoutée au stock.</b>", parse_mode=ParseMode.HTML, reply_markup=menu()); return True
    if flow == "commande_add":
        try: f["montant"] = _safe_float(f["montant"])
        except ValueError: await update.effective_message.reply_text("❌ Montant invalide.", reply_markup=back_menu()); return True
        DB["commandes"].append({"id":f"CMD-{len(DB['commandes'])+1:05d}", **f, "date":now_iso()})
    elif flow == "livraison_add": DB["livraisons"].append({"id":f"LIV-{len(DB['livraisons'])+1:05d}", **f, "date":now_iso()})
    elif flow == "fournisseur_add_v2": DB["fournisseurs"].append({"id":f"SUP-{len(DB['fournisseurs'])+1:05d}", **f, "created_at":now_iso()})
    elif flow in {"movement_in", "movement_out"}:
        try: qty = _safe_int(f["quantite"])
        except ValueError: await update.effective_message.reply_text("❌ Quantité invalide.", reply_markup=back_menu()); return True
        item = next((x for x in DB["stock"] if str(x.get("reference")) == str(f["reference"])), None)
        if not item: await update.effective_message.reply_text("❌ Référence stock introuvable.", reply_markup=back_menu()); return True
        if qty <= 0 or (flow == "movement_out" and int(item.get("quantite",0)) < qty): await update.effective_message.reply_text("❌ Quantité invalide ou stock insuffisant.", reply_markup=back_menu()); return True
        item["quantite"] = int(item.get("quantite",0)) + qty if flow == "movement_in" else int(item.get("quantite",0)) - qty
        item["updated_at"] = now_iso(); DB["mouvements"].append({"date":now_iso(),"reference":f["reference"],"type":"ENTREE" if flow=="movement_in" else "SORTIE","quantite":qty,"chat_id":cid})
    elif flow == "reparation":
        try: f["devis"] = _safe_float(f["devis"])
        except ValueError: await update.effective_message.reply_text("❌ Prix/devis invalide.", reply_markup=back_menu()); return True
        DB["reparations"].append({"id":f"REP-{len(DB['reparations'])+1:05d}", "numero":f["numero"], "appareil":f["appareil"], "client":f["client"], "panne":f["panne"], "identifiant":"" if f["imei"]=="-" else f["imei"], "type_identifiant":"IMEI" if f["imei"]!="-" else "", "devis":f["devis"], "statut":f["statut"], "date":now_iso(), "historique":[]})
    elif flow == "rupture_add":
        ref = str(f.get("reference", "")).strip()
        item = next((x for x in DB.get("stock", []) if str(x.get("reference", "")).strip().lower() == ref.lower()), None)
        if not item:
            await update.effective_message.reply_text(
                "❌ Référence introuvable dans le stock. Ajoute d'abord le produit au stock.",
                reply_markup=back_menu()
            )
            return True
        old = int(item.get("quantite", 0))
        item["quantite"] = 0
        item["updated_at"] = now_iso()
        DB.setdefault("mouvements", []).append({
            "date": now_iso(), "reference": item.get("reference"),
            "type": "RUPTURE_FORCEE", "quantite": -old,
            "avant": old, "apres": 0, "chat_id": cid
        })
        log_activity(cid, "RUPTURE_FORCEE", str(item.get("reference")))
    elif flow == "deblocage":
        if f["type"].upper() not in {"FRP", "GOOGLE", "FRP / GOOGLE", "ICLOUD", "I-CLOUD", "ICLOUD / APPLE"}:
            await update.effective_message.reply_text("❌ Réponds FRP ou iCloud.", reply_markup=back_menu()); return True
        try: f["montant"] = _safe_float(f["montant"])
        except ValueError: await update.effective_message.reply_text("❌ Prix invalide.", reply_markup=back_menu()); return True
        f["type"] = "FRP / Google" if "FRP" in f["type"].upper() or "GOOGLE" in f["type"].upper() else "iCloud / Apple"
        f["imei"] = "" if f["imei"] == "-" else f["imei"]; f["statut"] = "EN ATTENTE"; f["date"] = now_iso(); f["numero"] = f"DB-{len(DB['deblocages'])+1:05d}"
        DB["deblocages"].append(f.copy())
    else:
        return False
    save_db(DB); context.user_data.clear()
    labels={"commande_add":"commande","livraison_add":"livraison","fournisseur_add_v2":"fournisseur","movement_in":"entrée stock","movement_out":"sortie stock","reparation":"réparation","deblocage":"dossier de déblocage","rupture_add":"mise en rupture"}
    await update.effective_message.reply_text(f"✅ <b>{labels[flow].capitalize()} enregistré(e).</b>", parse_mode=ParseMode.HTML, reply_markup=menu())
    return True


async def v2_text_router(update, context):
    if not update.effective_message or not authorized(update.effective_chat.id): return False
    txt = update.effective_message.text.strip()

    if context.user_data.get("v2_search"):
        sec = context.user_data.pop("v2_search")
        await update.effective_message.reply_text(v2_text(sec, txt, current_chat_id=update.effective_chat.id), reply_markup=v2_keyboard(sec), parse_mode=ParseMode.HTML); return True

    if context.user_data.get("v2_search_action"):
        sec, act = context.user_data.pop("v2_search_action")
        items = DB.get(sec, [])
        found = next((x for x in items if txt.lower() in " ".join(str(v) for v in x.values()).lower()), None)
        if not found: await update.effective_message.reply_text("❌ Élément introuvable.", reply_markup=v2_keyboard(sec)); return True
        if act == "delete":
            items.remove(found); save_db(DB); await update.effective_message.reply_text("🗑️ Élément supprimé.", reply_markup=v2_keyboard(sec)); return True
        context.user_data["v2_edit_target"]=(sec, found)
        await update.effective_message.reply_text("✏️ Envoie le nouveau texte pour remplacer le champ <b>statut</b> (ou la valeur à corriger).", parse_mode=ParseMode.HTML, reply_markup=back_menu()); return True

    if context.user_data.get("v2_edit_target"):
        sec, found = context.user_data.pop("v2_edit_target"); found["statut"] = txt; save_db(DB); await update.effective_message.reply_text("✅ Élément modifié.", reply_markup=v2_keyboard(sec)); return True

    if context.user_data.get("v2_flow") == "collaborateur_add":
        if not can_manage_users(update.effective_chat.id): context.user_data.clear(); await update.effective_message.reply_text("🔒 Action réservée à l’administrateur ou au modérateur.", reply_markup=menu()); return True
        if not re.fullmatch(r"-?\d+", txt): await update.effective_message.reply_text("❌ Chat ID invalide. Envoie uniquement le numéro.", reply_markup=back_menu()); return True
        cid = str(txt); old = DB.get("users", {}).get(cid) or {}
        DB.setdefault("users", {})[cid] = {**old, "role":"collaborateur", "name":old.get("name","Collaborateur"), "username":old.get("username",""), "added_at":old.get("added_at",now_iso())}
        save_db(DB); context.user_data.clear()
        await update.effective_message.reply_text(f"✅ Collaborateur <code>{esc(cid)}</code> ajouté.\n\nIl doit utiliser /start sur le bot.", parse_mode=ParseMode.HTML, reply_markup=menu()); return True

    if context.user_data.get("v2_flow") == "collaborateur_role":
        if not admin(update.effective_chat.id):
            context.user_data.clear(); await update.effective_message.reply_text("🔒 Seul l’administrateur peut gérer les rôles.", reply_markup=menu()); return True
        needle=txt.lower(); found=None
        for cid,rec in _user_rows():
            hay=" ".join([cid,str(rec.get("name","")),str(rec.get("username",""))]).lower()
            if needle in hay: found=(cid,rec); break
        if not found:
            context.user_data.clear(); await update.effective_message.reply_text("❌ Collaborateur introuvable.", reply_markup=menu()); return True
        cid,rec=found
        if cid == str(ADMIN_CHAT_ID) or rec.get("role") == "admin":
            context.user_data.clear(); await update.effective_message.reply_text("🔒 Le compte administrateur ne peut pas être rétrogradé.", reply_markup=menu()); return True
        new_role = "moderateur" if rec.get("role") != "moderateur" else "collaborateur"
        rec["role"] = new_role
        save_db(DB)
        context.user_data.clear()
        label = "🛡️ Modérateur" if new_role == "moderateur" else "👤 Collaborateur"
        await update.effective_message.reply_text(
            f"✅ <b>{esc(rec.get('name','Collaborateur'))}</b> est maintenant <b>{label}</b>.\n\n"
            "Le modérateur peut ajouter et révoquer des collaborateurs, mais ne peut pas gérer les rôles ni l'administrateur.",
            parse_mode=ParseMode.HTML, reply_markup=menu()
        ); return True

    if context.user_data.get("v2_flow") == "collaborateur_revoke":
        if not can_manage_users(update.effective_chat.id): context.user_data.clear(); await update.effective_message.reply_text("🔒 Action réservée à l’administrateur ou au modérateur.", reply_markup=menu()); return True
        needle=txt.lower(); found=None
        for cid,rec in _user_rows():
            hay=" ".join([cid,str(rec.get("name","")),str(rec.get("username",""))]).lower()
            if needle in hay: found=(cid,rec); break
        if not found: context.user_data.clear(); await update.effective_message.reply_text("❌ Collaborateur introuvable.", reply_markup=menu()); return True
        cid,rec=found
        if cid == str(ADMIN_CHAT_ID) or rec.get("role") == "admin":
            context.user_data.clear(); await update.effective_message.reply_text("🔒 Impossible de révoquer l’administrateur.", reply_markup=menu()); return True
        if moderator(update.effective_chat.id) and rec.get("role") == "moderateur":
            context.user_data.clear(); await update.effective_message.reply_text("🔒 Un modérateur ne peut pas révoquer un autre modérateur.", reply_markup=menu()); return True
        DB["users"].pop(cid,None); save_db(DB); context.user_data.clear(); await update.effective_message.reply_text(f"🗑️ {esc(rec.get('name','Collaborateur'))} révoqué.", parse_mode=ParseMode.HTML, reply_markup=menu()); return True

    if context.user_data.get("v2_status"):
        st=context.user_data.pop("v2_status"); needle=txt.lower()
        for x in DB.get("reparations",[]):
            if needle in " ".join(str(v) for v in x.values()).lower():
                old=x.get("statut",""); x["statut"]=st; x.setdefault("historique",[]).append({"date":now_iso(),"action":"statut","ancien":old,"nouveau":st,"user":str(update.effective_chat.id)}); save_db(DB); await update.effective_message.reply_text(f"✅ {esc(old)} → <b>{esc(st)}</b>", parse_mode=ParseMode.HTML, reply_markup=menu()); return True
        await update.effective_message.reply_text("❌ Réparation introuvable.", reply_markup=menu()); return True

    # Mode inventaire : contrôle physique référence par référence.
    if context.user_data.get("inventory_items") is not None:
        items_refs = context.user_data.get("inventory_items", [])
        idx = int(context.user_data.get("inventory_index", 0))
        if idx >= len(items_refs):
            return True
        try:
            counted = _safe_int(txt)
            if counted < 0:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text("❌ Envoie uniquement une quantité entière positive ou 0.", reply_markup=back_menu())
            return True

        ref = items_refs[idx]
        item = next((x for x in DB.get("stock", []) if str(x.get("reference", "")) == ref), None)
        if item is None:
            await update.effective_message.reply_text("❌ Référence introuvable pendant l'inventaire.", reply_markup=back_menu())
            context.user_data.clear()
            return True
        old = int(item.get("quantite", 0))
        if old != counted:
            item["quantite"] = counted
            item["updated_at"] = now_iso()
            DB.setdefault("mouvements", []).append({
                "date": now_iso(), "reference": ref, "type": "INVENTAIRE",
                "quantite": counted - old, "avant": old, "apres": counted,
                "chat_id": update.effective_chat.id
            })
            context.user_data.setdefault("inventory_corrections", []).append((ref, old, counted))
            log_activity(update.effective_chat.id, "INVENTAIRE_CORRECTION", f"{ref}: {old} -> {counted}")

        idx += 1
        context.user_data["inventory_index"] = idx
        if idx < len(items_refs):
            next_ref = items_refs[idx]
            next_item = next((x for x in DB.get("stock", []) if str(x.get("reference", "")) == next_ref), None)
            theoretical = int(next_item.get("quantite", 0)) if next_item else 0
            await update.effective_message.reply_text(
                f"📋 <b>INVENTAIRE</b>\n\n{idx+1}/{len(items_refs)} — <b>{esc(next_item.get('produit', next_ref) if next_item else next_ref)}</b>\n"
                f"🔖 Réf. : <code>{esc(next_ref)}</code>\n"
                f"📦 Stock théorique : <b>{theoretical}</b>\n\n"
                "Envoie la quantité réellement comptée.",
                parse_mode=ParseMode.HTML, reply_markup=back_menu()
            )
            return True

        corrections = context.user_data.get("inventory_corrections", [])
        save_db(DB)
        context.user_data.clear()
        total = sum(int(x.get("quantite", 0)) for x in DB.get("stock", []))
        low = sum(1 for x in DB.get("stock", []) if 0 < int(x.get("quantite", 0)) <= int(x.get("seuil", DB["settings"]["low_stock_default"])))
        out = sum(1 for x in DB.get("stock", []) if int(x.get("quantite", 0)) <= 0)
        await update.effective_message.reply_text(
            "📋 <b>INVENTAIRE TERMINÉ</b>\n\n"
            f"📦 Références vérifiées : <b>{len(items_refs)}</b>\n"
            f"✏️ Corrections : <b>{len(corrections)}</b>\n"
            f"📦 Stock final : <b>{total}</b> unités\n"
            f"🟠 Stocks faibles : <b>{low}</b>\n"
            f"🔴 Ruptures : <b>{out}</b>",
            parse_mode=ParseMode.HTML, reply_markup=menu()
        )
        return True

    flow=context.user_data.get("v2_flow")
    if flow:
        steps=context.user_data.get("v2_steps",[]); step=context.user_data.get("v2_step",1); f=context.user_data.setdefault("v2_form",{})
        if step <= len(steps):
            key,prompt=steps[step-1]
            if flow == "deblocage" and key == "type":
                if txt.upper() not in {"FRP","GOOGLE","FRP / GOOGLE","ICLOUD","I-CLOUD","ICLOUD / APPLE"}:
                    await update.effective_message.reply_text("❌ Réponds <b>FRP</b> ou <b>iCloud</b>.", parse_mode=ParseMode.HTML, reply_markup=back_menu()); return True
            f[key]=txt; context.user_data["v2_step"]=step+1
            if step == len(steps):
                await _finish_flow(update,context,flow,f)
            else:
                await update.effective_message.reply_text(steps[step][1], parse_mode=ParseMode.HTML, reply_markup=back_menu())
            return True
    return False

async def v2_text_wrapper(update,context):
    if await v2_text_router(update,context): return
    await search_message(update,context)

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    stock_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_stock, pattern="^add_stock$")],
        states={
            ADD_STOCK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_stock_flow)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    supplier_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_supplier, pattern="^add_supplier$")],
        states={
            ADD_SUPPLIER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_supplier)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("access", access))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("in", quick_in))
    app.add_handler(CommandHandler("out", quick_out))
    app.add_handler(CommandHandler("scan", start_scanner))
    app.add_handler(CommandHandler("stopscan", stop_scanner))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(stock_conv)
    app.add_handler(supplier_conv)
    app.add_handler(CallbackQueryHandler(v2_handler, pattern=r"^v2(menu|act):"))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, scanner_webapp))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, v2_text_wrapper))

    return app


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Erreur Telegram", exc_info=context.error)


async def start_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["step"] = "stock_produit"
    await q.answer()
    if not authorized(update.effective_chat.id):
        return ConversationHandler.END
    await q.edit_message_text(
        "➕ <b>AJOUT STOCK</b>\n\nEnvoie le nom du produit/modèle.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu(),
    )
    return ADD_STOCK


async def start_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["step"] = "supplier_nom"
    await q.answer()
    if not authorized(update.effective_chat.id):
        return ConversationHandler.END
    await q.edit_message_text(
        "🏢 <b>NOUVEAU FOURNISSEUR</b>\n\nEnvoie le nom.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu(),
    )
    return ADD_SUPPLIER


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN manquant. Configure la variable d'environnement BOT_TOKEN."
        )
    if not ATELIER_PASSWORD:
        raise RuntimeError(
            "ATELIER_PASSWORD manquant. Configure le mot de passe collaborateur."
        )

    if not isinstance(DB.get('deblocages'), list):
        DB['deblocages'] = []

    # Initialise la table de correspondance des codes si elle n'existe pas encore.
    if not isinstance(DB.get("codes_stock"), dict):
        DB["codes_stock"] = {}

    # Garantit la présence de l'admin après chaque redémarrage.
    DB["users"][str(ADMIN_CHAT_ID)] = {
        **DB["users"].get(str(ADMIN_CHAT_ID), {}),
        "role": "admin",
        "name": DB["users"].get(str(ADMIN_CHAT_ID), {}).get("name", "Administrateur"),
    }
    save_db(DB, make_backup=False)

    app = build_app()
    app.add_error_handler(error_handler)
    log.info("AtelierBot PRO démarré. Admin=%s", ADMIN_CHAT_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
