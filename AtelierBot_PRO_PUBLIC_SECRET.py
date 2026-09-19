import os
import base64
import io
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
        [InlineKeyboardButton("📋 Inventaire", callback_data="v2act:stock:inventory")],
        [InlineKeyboardButton("💰 Comptabilité", callback_data="compta:menu")],
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
    revenue_unlocks = [x for x in unlocks if str(x.get("statut", "")).upper() != "ANNULÉE"]
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

    # 💰 Comptabilité : délégation vers le module comptable
    if action.startswith("compta:"):
        # Le module utilise la même DB persistante que le reste du bot.
        context.application.bot_data["db"] = DB
        # compta_handle_callback appelle q.answer(); celui-ci a déjà été appelé
        # par callback(), donc on traite directement les actions ici sans refaire answer.
        compta_action = action.split(":", 1)[1]
        if compta_action == "menu":
            await q.edit_message_text(compta_summary_text(DB), reply_markup=compta_menu_keyboard(), parse_mode="HTML")
            return
        if compta_action in {"invoices","credits","payments","expenses","bank"}:
            await q.edit_message_text(compta_summary_text(DB), reply_markup=compta_section_keyboard(compta_action), parse_mode="HTML")
            return
        # Pour les autres actions, utiliser le gestionnaire dédié.
        return await compta_handle_callback(update, context)

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
                f"🏷️ Étiquette : <code>{esc(x.get('etiquette', '-'))}</code>\n"
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
 "stock": ("📦 STOCK", [("➕ Ajouter", "v2act:stock:add"), ("📋 Voir", "v2act:stock:list"), ("🔎 Rechercher", "v2act:stock:search"), ("✏️ Modifier", "v2act:stock:edit"), ("🗑️ Supprimer", "v2act:stock:delete")]),
 "ruptures": ("🚨 RUPTURES", [("➕ Ajouter", "v2act:ruptures:add"), ("📋 Voir", "v2act:ruptures:list"), ("🔎 Rechercher", "v2act:ruptures:search"), ("↩️ Annuler la rupture", "v2act:ruptures:cancel")]),
 "commandes": ("📋 COMMANDES", [("➕ Ajouter", "v2act:commandes:add"), ("📋 Voir", "v2act:commandes:list"), ("🔎 Rechercher", "v2act:commandes:search"), ("✏️ Modifier", "v2act:commandes:edit"), ("🗑️ Supprimer", "v2act:commandes:delete")]),
 "livraisons": ("🚚 LIVRAISONS", [("➕ Ajouter", "v2act:livraisons:add"), ("📋 Voir", "v2act:livraisons:list"), ("🔎 Rechercher", "v2act:livraisons:search"), ("✏️ Modifier", "v2act:livraisons:edit"), ("🗑️ Supprimer", "v2act:livraisons:delete")]),
 "reparations": ("🔧 RÉPARATIONS", [("➕ Ajouter une réparation", "v2act:reparations:add"), ("📋 Voir", "v2act:reparations:list"), ("🔎 Rechercher", "v2act:reparations:search"), ("✏️ Modifier", "v2act:reparations:edit"), ("🗑️ Supprimer", "v2act:reparations:delete"), ("⏸️ Pause", "v2act:reparations:pause"), ("▶️ Reprendre", "v2act:reparations:resume"), ("📦 Attente pièce", "v2act:reparations:parts"), ("👤 Attente client", "v2act:reparations:customer"), ("🧪 À tester", "v2act:reparations:test"), ("✅ Terminer", "v2act:reparations:done"), ("📦 Livrée", "v2act:reparations:delivered"), ("❌ Annuler", "v2act:reparations:cancel")]),
 "deblocages": ("🔓 DÉBLOCAGES", [("➕ Nouveau dossier", "v2act:deblocages:add"), ("📋 Voir", "v2act:deblocages:list"), ("🔎 Rechercher", "v2act:deblocages:search"), ("✏️ Modifier", "v2act:deblocages:edit"), ("🗑️ Supprimer", "v2act:deblocages:delete"), ("❌ Annuler", "v2act:deblocages:cancel")]),
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
        context.user_data["inventory_results"] = []
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

    if sec == "ruptures" and act == "cancel":
        context.user_data["v2_rupture_cancel"] = True
        await q.edit_message_text(
            "↩️ <b>ANNULER UNE RUPTURE</b>\n\n"
            "Envoie la référence du produit.\n"
            "Le bot cherchera la dernière rupture forcée de cette référence "
            "et restaurera le stock qu'elle avait avant la rupture.",
            parse_mode=ParseMode.HTML, reply_markup=back_menu()
        ); return

    if sec == "deblocages" and act == "cancel":
        context.user_data["v2_deblocage_cancel"] = True
        await q.edit_message_text(
            "❌ <b>ANNULER UN DÉBLOCAGE</b>\n\n"
            "Envoie le numéro du dossier (ex. DB-00001), l'IMEI ou un élément permettant de l'identifier.",
            parse_mode=ParseMode.HTML, reply_markup=back_menu()
        ); return

    flow_specs = {
        ("commandes", "add"): ("commande_add", [("numero", "1/4 — Numéro de commande ?"), ("fournisseur", "2/4 — Fournisseur ?"), ("montant", "3/4 — Montant ?"), ("statut", "4/4 — Statut ?")], "📋 <b>NOUVELLE COMMANDE</b>\n\n1/4 — Numéro de commande ?"),
        ("livraisons", "add"): ("livraison_add", [("commande", "1/5 — Numéro de commande ?"), ("transporteur", "2/5 — Transporteur ?"), ("suivi", "3/5 — Numéro de suivi ?"), ("date_prevue", "4/5 — Date prévue ?"), ("statut", "5/5 — Statut ?")], "🚚 <b>NOUVELLE LIVRAISON</b>\n\n1/5 — Numéro de commande ?"),
        ("fournisseurs", "add"): ("fournisseur_add_v2", [("nom", "1/4 — Nom du fournisseur ?"), ("contact", "2/4 — Contact ?"), ("telephone", "3/4 — Téléphone ?"), ("email", "4/4 — E-mail (ou -) ?")], "🏢 <b>NOUVEAU FOURNISSEUR</b>\n\n1/4 — Nom du fournisseur ?"),
        ("mouvements", "in"): ("movement_in", [("reference", "1/2 — Référence stock ?"), ("quantite", "2/2 — Quantité à entrer ?")], "📥 <b>ENTRÉE STOCK</b>\n\n1/2 — Référence stock ?"),
        ("mouvements", "out"): ("movement_out", [("reference", "1/2 — Référence stock ?"), ("quantite", "2/2 — Quantité à sortir ?")], "📤 <b>SORTIE STOCK</b>\n\n1/2 — Référence stock ?"),
        ("reparations", "add"): ("reparation", [("numero", "1/8 — Numéro de réparation ?"), ("appareil", "2/8 — Modèle/appareil ?"), ("client", "3/8 — Client ?"), ("panne", "4/8 — Panne ?"), ("imei", "5/8 — IMEI (ou -) ?"), ("etiquette", "6/8 — Numéro d’étiquette hologramme ?"), ("devis", "7/8 — Prix/devis ?"), ("statut", "8/8 — Statut ?")], "🔧 <b>NOUVELLE RÉPARATION</b>\n\n1/8 — Numéro de réparation ?"),
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
        etiquette = str(f.get("etiquette", "")).strip()
        if not etiquette or etiquette == "-":
            await update.effective_message.reply_text(
                "❌ Le numéro d’étiquette hologramme est obligatoire pour une réparation.\n\n"
                "Envoie le numéro unique indiqué sur l’étiquette.",
                reply_markup=back_menu()
            )
            context.user_data["v2_step"] = 6
            return True
        duplicate = next((x for x in DB.get("reparations", []) if str(x.get("etiquette", "")).strip().lower() == etiquette.lower()), None)
        if duplicate:
            await update.effective_message.reply_text(
                f"❌ Cette étiquette hologramme est déjà utilisée sur la réparation <b>{esc(duplicate.get('numero',''))}</b>.\n\n"
                "Chaque étiquette doit être unique. Envoie un autre numéro.",
                parse_mode=ParseMode.HTML, reply_markup=back_menu()
            )
            context.user_data["v2_step"] = 6
            return True
        try:
            f["devis"] = _safe_float(f["devis"])
        except ValueError:
            context.user_data["v2_step"] = 7
            await update.effective_message.reply_text("❌ Prix/devis invalide. Réenvoie le montant.", reply_markup=back_menu())
            return True
        DB["reparations"].append({"id":f"REP-{len(DB['reparations'])+1:05d}", "numero":f["numero"], "appareil":f["appareil"], "client":f["client"], "panne":f["panne"], "identifiant":"" if f["imei"]=="-" else f["imei"], "type_identifiant":"IMEI" if f["imei"]!="-" else "", "etiquette":etiquette, "devis":f["devis"], "statut":f["statut"], "date":now_iso(), "historique":[]})
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

    if context.user_data.get("v2_rupture_cancel"):
        context.user_data.pop("v2_rupture_cancel", None)
        ref = txt.strip()
        candidate = next(
            (
                m for m in reversed(DB.get("mouvements", []))
                if str(m.get("type", "")).upper() == "RUPTURE_FORCEE"
                and str(m.get("reference", "")).strip().lower() == ref.lower()
            ),
            None,
        )
        if not candidate:
            await update.effective_message.reply_text(
                "❌ Aucune rupture forcée trouvée pour cette référence.",
                reply_markup=v2_keyboard("ruptures")
            ); return True
        item = next(
            (x for x in DB.get("stock", []) if str(x.get("reference", "")).strip().lower() == ref.lower()),
            None,
        )
        if not item:
            await update.effective_message.reply_text("❌ Référence stock introuvable.", reply_markup=v2_keyboard("ruptures")); return True
        current = int(item.get("quantite", 0))
        before = int(candidate.get("avant", 0))
        item["quantite"] = before
        item["updated_at"] = now_iso()
        DB.setdefault("mouvements", []).append({
            "date": now_iso(), "reference": item.get("reference"),
            "type": "ANNULATION_RUPTURE", "quantite": before - current,
            "avant": current, "apres": before, "chat_id": update.effective_chat.id
        })
        log_activity(update.effective_chat.id, "ANNULATION_RUPTURE", str(item.get("reference")))
        save_db(DB)
        await update.effective_message.reply_text(
            f"↩️ <b>Rupture annulée.</b>\n\n"
            f"🔖 Référence : <code>{esc(item.get('reference'))}</code>\n"
            f"📦 Stock restauré : <b>{before}</b>",
            parse_mode=ParseMode.HTML, reply_markup=v2_keyboard("ruptures")
        ); return True

    if context.user_data.get("v2_deblocage_cancel"):
        context.user_data.pop("v2_deblocage_cancel", None)
        needle = txt.lower()
        found = next(
            (
                x for x in DB.get("deblocages", [])
                if needle in " ".join(str(v) for v in x.values()).lower()
            ),
            None,
        )
        if not found:
            await update.effective_message.reply_text("❌ Dossier de déblocage introuvable.", reply_markup=v2_keyboard("deblocages")); return True
        old = found.get("statut", "EN ATTENTE")
        if str(old).upper() == "ANNULÉE":
            await update.effective_message.reply_text("ℹ️ Ce dossier est déjà annulé.", reply_markup=v2_keyboard("deblocages")); return True
        found["statut"] = "ANNULÉE"
        found.setdefault("historique", []).append({
            "date": now_iso(), "action": "annulation",
            "ancien": old, "nouveau": "ANNULÉE",
            "user": str(update.effective_chat.id)
        })
        save_db(DB)
        await update.effective_message.reply_text(
            f"❌ <b>Dossier annulé.</b>\n\n🔖 {esc(found.get('numero', ''))}",
            parse_mode=ParseMode.HTML, reply_markup=v2_keyboard("deblocages")
        ); return True

    if context.user_data.get("v2_search_action"):
        sec, act = context.user_data.pop("v2_search_action")
        items = DB.get(sec, [])
        found = next((x for x in items if txt.lower() in " ".join(str(v) for v in x.values()).lower()), None)
        if not found:
            await update.effective_message.reply_text("❌ Élément introuvable.", reply_markup=v2_keyboard(sec)); return True
        if act == "delete":
            items.remove(found)
            save_db(DB)
            await update.effective_message.reply_text("🗑️ Élément supprimé.", reply_markup=v2_keyboard(sec))
            return True
        if sec == "deblocages":
            context.user_data["v2_deblocage_edit_target"] = found
            await update.effective_message.reply_text(
                "✏️ <b>MODIFIER LE DÉBLOCAGE</b>\n\n"
                "Quel champ veux-tu modifier ?\n"
                "• <b>type</b> — FRP / Google ou iCloud / Apple\n"
                "• <b>appareil</b>\n"
                "• <b>imei</b>\n"
                "• <b>client</b>\n"
                "• <b>montant</b>\n"
                "• <b>statut</b>",
                parse_mode=ParseMode.HTML, reply_markup=back_menu()
            ); return True
        context.user_data["v2_edit_target"]=(sec, found)
        await update.effective_message.reply_text("✏️ Envoie le nouveau texte pour remplacer le champ <b>statut</b> (ou la valeur à corriger).", parse_mode=ParseMode.HTML, reply_markup=back_menu()); return True

    if context.user_data.get("v2_deblocage_edit_target") and not context.user_data.get("v2_deblocage_edit_field"):
        found = context.user_data["v2_deblocage_edit_target"]
        field = txt.strip().lower()
        allowed = {"type", "appareil", "imei", "client", "montant", "statut"}
        if field not in allowed:
            await update.effective_message.reply_text("❌ Champ invalide. Choisis : type, appareil, imei, client, montant ou statut.", reply_markup=back_menu()); return True
        context.user_data["v2_deblocage_edit_field"] = field
        prompts = {
            "type": "Envoie FRP / Google ou iCloud / Apple.",
            "appareil": "Envoie le nouveau modèle/appareil.",
            "imei": "Envoie le nouvel IMEI (ou -).",
            "client": "Envoie le nouveau client.",
            "montant": "Envoie le nouveau montant.",
            "statut": "Envoie le nouveau statut (EN ATTENTE, EN COURS, TERMINÉE, ANNULÉE, etc.).",
        }
        await update.effective_message.reply_text("✏️ " + prompts[field], parse_mode=ParseMode.HTML, reply_markup=back_menu()); return True

    if context.user_data.get("v2_deblocage_edit_field"):
        field = context.user_data.pop("v2_deblocage_edit_field")
        found = context.user_data.pop("v2_deblocage_edit_target")
        if field == "montant":
            try:
                found["montant"] = _safe_float(txt)
            except ValueError:
                context.user_data["v2_deblocage_edit_field"] = field
                context.user_data["v2_deblocage_edit_target"] = found
                await update.effective_message.reply_text("❌ Montant invalide. Réessaie.", reply_markup=back_menu()); return True
        elif field == "type":
            upper = txt.upper()
            if upper not in {"FRP", "GOOGLE", "FRP / GOOGLE", "ICLOUD", "I-CLOUD", "ICLOUD / APPLE"}:
                context.user_data["v2_deblocage_edit_field"] = field
                context.user_data["v2_deblocage_edit_target"] = found
                await update.effective_message.reply_text("❌ Réponds FRP / Google ou iCloud / Apple.", reply_markup=back_menu()); return True
            found["type"] = "FRP / Google" if "FRP" in upper or "GOOGLE" in upper else "iCloud / Apple"
        elif field == "imei":
            found["imei"] = "" if txt.strip() == "-" else txt.strip()
        else:
            found[field] = txt.strip()
        found.setdefault("historique", []).append({
            "date": now_iso(), "action": "modification",
            "champ": field, "nouvelle_valeur": found.get(field),
            "user": str(update.effective_chat.id)
        })
        save_db(DB)
        await update.effective_message.reply_text("✅ <b>Dossier de déblocage modifié.</b>", parse_mode=ParseMode.HTML, reply_markup=v2_keyboard("deblocages")); return True

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
            await update.effective_message.reply_text(
                "❌ Envoie uniquement une quantité entière positive ou 0.",
                reply_markup=back_menu(),
            )
            return True

        ref = items_refs[idx]
        item = next(
            (x for x in DB.get("stock", []) if str(x.get("reference", "")) == ref),
            None,
        )
        if item is None:
            await update.effective_message.reply_text(
                "❌ Référence introuvable pendant l'inventaire.",
                reply_markup=back_menu(),
            )
            context.user_data.clear()
            return True

        theoretical = int(item.get("quantite", 0))
        ecart = counted - theoretical

        # On conserve le détail de chaque référence contrôlée.
        result = {
            "reference": ref,
            "produit": str(item.get("produit", ref)),
            "theorique": theoretical,
            "compte": counted,
            "ecart": ecart,
        }
        context.user_data.setdefault("inventory_results", []).append(result)

        if theoretical != counted:
            item["quantite"] = counted
            item["updated_at"] = now_iso()

            DB.setdefault("mouvements", []).append(
                {
                    "date": now_iso(),
                    "reference": ref,
                    "type": "INVENTAIRE",
                    "quantite": ecart,
                    "avant": theoretical,
                    "apres": counted,
                    "chat_id": update.effective_chat.id,
                }
            )

            context.user_data.setdefault("inventory_corrections", []).append(result)
            log_activity(
                update.effective_chat.id,
                "INVENTAIRE_CORRECTION",
                f"{ref}: {theoretical} -> {counted} (écart {ecart:+d})",
            )

        # Affiche immédiatement l'écart de la référence qui vient d'être comptée.
        ecart_label = "Aucun écart" if ecart == 0 else f"{ecart:+d}"
        ecart_icon = "✅" if ecart == 0 else "⚠️"

        idx += 1
        context.user_data["inventory_index"] = idx

        if idx < len(items_refs):
            next_ref = items_refs[idx]
            next_item = next(
                (
                    x
                    for x in DB.get("stock", [])
                    if str(x.get("reference", "")) == next_ref
                ),
                None,
            )
            next_theoretical = (
                int(next_item.get("quantite", 0)) if next_item else 0
            )

            await update.effective_message.reply_text(
                f"📱 <b>{esc(item.get('produit', ref))}</b>\n"
                f"🔖 Réf. : <code>{esc(ref)}</code>\n"
                f"📦 Théorique : <b>{theoretical}</b>\n"
                f"📋 Compté : <b>{counted}</b>\n"
                f"{ecart_icon} Écart : <b>{esc(ecart_label)}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>INVENTAIRE</b> — {idx + 1}/{len(items_refs)}\n\n"
                f"📱 <b>{esc(next_item.get('produit', next_ref) if next_item else next_ref)}</b>\n"
                f"🔖 Réf. : <code>{esc(next_ref)}</code>\n"
                f"📦 Stock théorique : <b>{next_theoretical}</b>\n\n"
                "Envoie la quantité réellement comptée.",
                parse_mode=ParseMode.HTML,
                reply_markup=back_menu(),
            )
            return True

        # Fin : sauvegarde persistante et recalcul complet à partir du stock final.
        results = context.user_data.get("inventory_results", [])
        corrections = [x for x in results if int(x["ecart"]) != 0]
        positive = sum(int(x["ecart"]) for x in results if int(x["ecart"]) > 0)
        negative = sum(abs(int(x["ecart"])) for x in results if int(x["ecart"]) < 0)

        # Les indicateurs sont recalculés depuis les quantités réellement enregistrées.
        stock = DB.get("stock", [])
        total_units = sum(int(x.get("quantite", 0)) for x in stock)
        low = sum(
            1
            for x in stock
            if 0 < int(x.get("quantite", 0))
            <= int(x.get("seuil", DB["settings"]["low_stock_default"]))
        )
        ruptures = sum(1 for x in stock if int(x.get("quantite", 0)) <= 0)
        stock_value = sum(
            int(x.get("quantite", 0)) * float(x.get("prix_achat", 0))
            for x in stock
        )

        # Une seule sauvegarde à la fin de l'inventaire : les corrections
        # survivent au redémarrage exactement comme les autres données.
        save_db(DB)

        context.user_data.clear()

        await update.effective_message.reply_text(
            "📋 <b>INVENTAIRE TERMINÉ</b>\n\n"
            f"📋 Références vérifiées : <b>{len(results)}</b>\n"
            f"✏️ Corrections : <b>{len(corrections)}</b>\n"
            f"➕ Écarts positifs : <b>{positive}</b>\n"
            f"➖ Écarts négatifs : <b>{negative}</b>\n\n"
            f"📦 Stock final : <b>{total_units}</b> unités\n"
            f"🟢 Références : <b>{len(stock)}</b>\n"
            f"🟠 Stocks faibles : <b>{low}</b>\n"
            f"🔴 Ruptures : <b>{ruptures}</b>\n"
            f"💰 Valeur d'achat du stock : <b>{money(stock_value)}</b>\n\n"
            "✅ Les corrections ont été enregistrées dans la base persistante.",
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
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
    # PDF / photos : interceptés avant le routeur texte pour les pièces jointes comptables.
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, compta_receive_attachment))
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


# ====== COMPTABILITE (module complementaire) ======
COMPTA_DEFAULT={"next_invoice":1,"next_credit_note":1,"next_attachment":1,"invoices":[],"credit_notes":[],
"payments":[],"expenses":[],"bank_lines":[],"documents":[],"vat_rate":20.0,"journal":[]}

def compta_data(db):
    c=db.setdefault("comptabilite",{})
    for k,v in COMPTA_DEFAULT.items():
        if k not in c: c[k]=[] if isinstance(v,list) else v
    return c

def compta_next_number(c,kind="invoice"):
    key="next_invoice" if kind=="invoice" else "next_credit_note"
    prefix="FA" if kind=="invoice" else "AV"
    n=int(c.get(key,1)); c[key]=n+1
    return f"{prefix}-{n:06d}"

def compta_add_invoice(db,customer,amount_ht,vat_rate=None,description=""):
    c=compta_data(db); rate=float(c["vat_rate"] if vat_rate is None else vat_rate)
    ht=round(float(amount_ht),2); vat=round(ht*rate/100,2); ttc=round(ht+vat,2)
    num=compta_next_number(c)
    row={"number":num,"customer":str(customer),"description":str(description),
    "date":__import__("datetime").date.today().isoformat(),"amount_ht":ht,
    "vat_rate":rate,"vat":vat,"amount_ttc":ttc,"status":"emise"}
    c["invoices"].append(row); c["journal"].append({"date":row["date"],"type":"FACTURE",
    "number":num,"label":description or customer,"debit":ttc,"credit":0.0}); return row

def compta_add_credit_note(db,customer,amount_ht,vat_rate=None,description=""):
    c=compta_data(db); rate=float(c["vat_rate"] if vat_rate is None else vat_rate)
    ht=round(float(amount_ht),2); vat=round(ht*rate/100,2); ttc=round(ht+vat,2)
    num=compta_next_number(c,"credit_note")
    row={"number":num,"customer":str(customer),"description":str(description),
    "date":__import__("datetime").date.today().isoformat(),"amount_ht":ht,
    "vat_rate":rate,"vat":vat,"amount_ttc":ttc,"status":"emise"}
    c["credit_notes"].append(row); c["journal"].append({"date":row["date"],"type":"AVOIR",
    "number":num,"label":description or customer,"debit":0.0,"credit":ttc}); return row

def compta_add_payment(db,amount,method="CB",reference="",invoice_number=""):
    c=compta_data(db)
    row={"date":__import__("datetime").date.today().isoformat(),"amount":round(float(amount),2),
    "method":str(method),"reference":str(reference),"invoice_number":str(invoice_number)}
    c["payments"].append(row); return row

def compta_add_expense(db,label,amount_ht,vat_rate=None,category=""):
    c=compta_data(db); rate=float(c["vat_rate"] if vat_rate is None else vat_rate)
    ht=round(float(amount_ht),2); vat=round(ht*rate/100,2); ttc=round(ht+vat,2)
    row={"date":__import__("datetime").date.today().isoformat(),"label":str(label),
    "category":str(category),"amount_ht":ht,"vat_rate":rate,"vat":vat,"amount_ttc":ttc}
    c["expenses"].append(row); c["journal"].append({"date":row["date"],"type":"DEPENSE",
    "number":"","label":label,"debit":0.0,"credit":ttc}); return row

def compta_add_bank_line(db,date,label,amount,reference="",matched=False):
    c=compta_data(db)
    row={"date":str(date),"label":str(label),"amount":round(float(amount),2),
    "reference":str(reference),"matched":bool(matched)}
    c["bank_lines"].append(row); return row

def compta_reconcile(db):
    c=compta_data(db); unmatched=[]
    for line in c["bank_lines"]:
        if line.get("matched"): continue
        ok=any(round(float(p.get("amount",0)),2)==round(float(line.get("amount",0)),2)
        and (not line.get("reference") or line.get("reference") in str(p.get("reference","")))
        for p in c["payments"])
        if ok: line["matched"]=True
        else: unmatched.append(line)
    return unmatched

def compta_vat_summary(db):
    c=compta_data(db)
    collected=round(sum(float(x.get("vat",0)) for x in c["invoices"]),2)
    deductible=round(sum(float(x.get("vat",0)) for x in c["expenses"]),2)
    credit=round(sum(float(x.get("vat",0)) for x in c["credit_notes"]),2)
    return {"collected":collected,"deductible":deductible,"credit_vat":credit,
    "net_due":round(collected-credit-deductible,2)}

def compta_export_fec(db):
    c=compta_data(db)
    fields=["JournalCode","JournalLib","EcritureNum","EcritureDate","CompteNum",
    "CompteLib","CompAuxNum","CompAuxLib","PieceRef","PieceDate","EcritureLib",
    "Debit","Credit","EcritureLet","DateLet","ValidDate","Montantdevise","Idevise"]
    rows=["\t".join(fields)]
    for i,e in enumerate(c["journal"],1):
        d=str(e.get("date","")).replace("-","")
        debit=f'{float(e.get("debit",0)):.2f}'.replace(".",",")
        credit=f'{float(e.get("credit",0)):.2f}'.replace(".",",")
        rows.append("\t".join(["AC","Comptabilite",str(i),d,"000000","Compte atelier",
        "","","",d,str(e.get("label","")),debit,credit,"","",d,"",""]))
    return "\n".join(rows)

# ============================================================
# 💰 INTERFACE TELEGRAM — COMPTABILITÉ
# ============================================================
COMPTA_UI_ACTIONS = {
    "invoice": "🧾 Facture",
    "credit": "↩️ Avoir",
    "payment": "💳 Encaissement",
    "expense": "💸 Dépense",
    "bank": "🏦 Banque",
    "vat": "🧮 TVA",
    "journal": "📚 Journal",
    "fec": "📤 Export FEC",
}

def compta_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Factures", callback_data="compta:invoices"),
         InlineKeyboardButton("↩️ Avoirs", callback_data="compta:credits")],
        [InlineKeyboardButton("💳 Encaissements", callback_data="compta:payments"),
         InlineKeyboardButton("💸 Dépenses", callback_data="compta:expenses")],
        [InlineKeyboardButton("🏦 Banque", callback_data="compta:bank"),
         InlineKeyboardButton("🧮 TVA", callback_data="compta:vat")],
        [InlineKeyboardButton("📚 Journal", callback_data="compta:journal"),
         InlineKeyboardButton("📤 Export FEC", callback_data="compta:fec")],
        [InlineKeyboardButton("📎 Pièces jointes", callback_data="compta:attachments")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="home")],
    ])

def compta_section_keyboard(section):
    if section == "invoices":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Nouvelle facture", callback_data="compta:add_invoice")],
            [InlineKeyboardButton("📎 Joindre un PDF / JPG", callback_data="compta:upload_invoice")],
            [InlineKeyboardButton("📋 Voir les factures", callback_data="compta:list_invoices")],
            [InlineKeyboardButton("📎 Voir / récupérer les fichiers", callback_data="compta:invoice_files")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="compta:menu")],
        ])
    if section == "credits":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Nouvel avoir", callback_data="compta:add_credit")],
            [InlineKeyboardButton("📋 Voir les avoirs", callback_data="compta:list_credits")],
            [InlineKeyboardButton("📎 Joindre un PDF / JPG", callback_data="compta:upload_credit")],
            [InlineKeyboardButton("📎 Voir / récupérer les fichiers", callback_data="compta:credit_files")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="compta:menu")],
        ])
    if section == "payments":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ajouter encaissement", callback_data="compta:add_payment")],
            [InlineKeyboardButton("📋 Voir les encaissements", callback_data="compta:list_payments")],
            [InlineKeyboardButton("📎 Joindre un PDF / JPG", callback_data="compta:upload_payment")],
            [InlineKeyboardButton("📎 Voir / récupérer les fichiers", callback_data="compta:payment_files")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="compta:menu")],
        ])
    if section == "expenses":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ajouter dépense", callback_data="compta:add_expense")],
            [InlineKeyboardButton("📋 Voir les dépenses", callback_data="compta:list_expenses")],
            [InlineKeyboardButton("📎 Joindre un PDF / JPG", callback_data="compta:upload_expense")],
            [InlineKeyboardButton("📎 Voir / récupérer les fichiers", callback_data="compta:expense_files")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="compta:menu")],
        ])
    if section == "bank":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ajouter ligne bancaire", callback_data="compta:add_bank")],
            [InlineKeyboardButton("🔄 Rapprocher", callback_data="compta:reconcile")],
            [InlineKeyboardButton("📋 Voir la banque", callback_data="compta:list_bank")],
            [InlineKeyboardButton("📎 Joindre un PDF / JPG", callback_data="compta:upload_bank")],
            [InlineKeyboardButton("📎 Voir / récupérer les fichiers", callback_data="compta:bank_files")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="compta:menu")],
        ])
    return compta_menu_keyboard()

def compta_format_money(v):
    return f"{float(v):,.2f} €".replace(",", " ").replace(".", ",")

def compta_summary_text(db):
    c = compta_data(db)
    vat = compta_vat_summary(db)
    total_invoices = sum(float(x.get("amount_ttc", 0)) for x in c["invoices"])
    total_expenses = sum(float(x.get("amount_ttc", 0)) for x in c["expenses"])
    payments = sum(float(x.get("amount", 0)) for x in c["payments"])
    return (
        "💰 <b>COMPTABILITÉ</b>\n\n"
        f"🧾 Factures : {len(c['invoices'])} — {compta_format_money(total_invoices)}\n"
        f"↩️ Avoirs : {len(c['credit_notes'])}\n"
        f"💳 Encaissements : {compta_format_money(payments)}\n"
        f"💸 Dépenses : {len(c['expenses'])} — {compta_format_money(total_expenses)}\n"
        f"🧮 TVA nette calculée : {compta_format_money(vat['net_due'])}\n"
        f"📚 Écritures : {len(c['journal'])}\n"
        f"🏦 Lignes bancaires : {len(c['bank_lines'])}"
    )

def compta_list_text(db, kind):
    c = compta_data(db)
    mapping = {
        "invoices": ("🧾 FACTURES", c["invoices"], lambda x:
            f"{x['number']} • {x['customer']} • {compta_format_money(x['amount_ttc'])}"),
        "credits": ("↩️ AVOIRS", c["credit_notes"], lambda x:
            f"{x['number']} • {x['customer']} • {compta_format_money(x['amount_ttc'])}"),
        "payments": ("💳 ENCAISSEMENTS", c["payments"], lambda x:
            f"{x['date']} • {x['method']} • {compta_format_money(x['amount'])}"),
        "expenses": ("💸 DÉPENSES", c["expenses"], lambda x:
            f"{x['date']} • {x['label']} • {compta_format_money(x['amount_ttc'])}"),
        "bank": ("🏦 BANQUE", c["bank_lines"], lambda x:
            f"{x['date']} • {x['label']} • {compta_format_money(x['amount'])} • {'✅' if x.get('matched') else '⏳'}"),
    }
    title, items, formatter = mapping[kind]
    if not items:
        return f"{title}\n\nAucun élément."
    return title + "\n\n" + "\n".join(formatter(x) for x in items[-30:])

async def compta_handle_callback(update, context):
    q = update.callback_query
    if not q or not q.data.startswith("compta:"):
        return False
    await q.answer()
    db = context.application.bot_data.get("db")
    if db is None:
        db = context.application.bot_data.setdefault("db", {})
    action = q.data.split(":", 1)[1]

    if action == "menu":
        await q.edit_message_text(compta_summary_text(db), reply_markup=compta_menu_keyboard(), parse_mode="HTML")
    elif action in {"invoices","credits","payments","expenses","bank"}:
        await q.edit_message_text(
            compta_list_text(db, action) if action.startswith("list_") is False else "",
            reply_markup=compta_section_keyboard(action), parse_mode="HTML"
        )
    elif action.startswith("list_"):
        kind = action[5:]
        await q.edit_message_text(compta_list_text(db, kind), reply_markup=compta_section_keyboard(kind), parse_mode="HTML")
    elif action == "vat":
        v = compta_vat_summary(db)
        await q.edit_message_text(
            "🧮 <b>TVA</b>\n\n"
            f"TVA collectée : {compta_format_money(v['collected'])}\n"
            f"TVA sur avoirs : {compta_format_money(v['credit_vat'])}\n"
            f"TVA déductible : {compta_format_money(v['deductible'])}\n"
            f"TVA nette calculée : {compta_format_money(v['net_due'])}\n\n"
            "⚠️ Calcul indicatif : le régime fiscal réel doit être vérifié avant déclaration.",
            reply_markup=compta_menu_keyboard(), parse_mode="HTML")
    elif action == "journal":
        c = compta_data(db)
        body = "📚 <b>JOURNAL</b>\n\n" + (
            "\n".join(f"{e.get('date','')} • {e.get('type','')} • {e.get('label','')} • "
                      f"D {compta_format_money(e.get('debit',0))} / C {compta_format_money(e.get('credit',0))}"
                      for e in c["journal"][-30:])
            if c["journal"] else "Aucune écriture."
        )
        await q.edit_message_text(body, reply_markup=compta_menu_keyboard(), parse_mode="HTML")
    elif action == "fec":
        fec = compta_export_fec(db)
        data = fec.encode("utf-8")
        attachment = compta_store_document(db, data, "export_fec.txt", "text/plain", "FEC", "Export FEC")
        save_db(DB)
        await q.message.reply_document(
            document=io.BytesIO(data),
            filename="export_fec.txt",
            caption="📤 Export FEC — fichier généré par AtelierBot.\n⚠️ À faire valider par l'expert-comptable avant utilisation comme FEC légal."
        )
        await q.edit_message_text(
            f"📤 <b>EXPORT FEC</b>\n\n"
            "✅ Le fichier vient d'être envoyé dans Telegram.\n"
            f"🆔 <code>{esc(attachment['id'])}</code>\n\n"
            "💾 Il est aussi conservé dans <code>data.json</code> pour être récupéré plus tard.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ Récupérer les fichiers enregistrés", callback_data="compta:attachments")],
                [InlineKeyboardButton("⬅️ Retour comptabilité", callback_data="compta:menu")]
            ]),
            parse_mode="HTML")
    elif action == "attachments":
        await compta_show_all_attachments(q, db)
    elif action in {"invoice_attachments","credit_attachments","payment_attachments","expense_attachments","bank_attachments"}:
        kind = action.split("_", 1)[0]
        await compta_choose_attachment_target(q, context, db, kind)
    elif action.startswith("upload_"):
        kind = action.split("_", 1)[1]
        context.user_data["compta_attachment_target"] = f"document|{kind}"
        await q.edit_message_text(
            f"📎 <b>AJOUTER UN FICHIER — {esc(kind.upper())}</b>\n\n"
            "Envoie maintenant le fichier depuis la 📎 pièce jointe Telegram (PDF, JPG, JPEG ou PNG).\n"
            "Le fichier sera enregistré intégralement dans <code>data.json</code> et pourra être récupéré plus tard directement dans Telegram.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📎 Pièces jointes enregistrées", callback_data="compta:attachments")],
                [InlineKeyboardButton("⬅️ Retour", callback_data=f"compta:{ {'invoice':'invoices','credit':'credits','payment':'payments','expense':'expenses','bank':'bank'}.get(kind,'menu')}")]
            ])
        )
    elif action.startswith("download_attachment:"):
        aid = action.split(":", 1)[1]
        await compta_send_attachment(q.message, db, aid)
    elif action in {"invoice_files","credit_files","payment_files","expense_files","bank_files"}:
        kind = action.split("_", 1)[0]
        await compta_show_attachments_by_kind(q, db, kind)
    elif action.startswith("attach_to:"):
        target = action.split(":", 1)[1]
        context.user_data["compta_attachment_target"] = target
        await q.edit_message_text(
            "📎 <b>AJOUTER UNE PIÈCE JOINTE</b>\n\n"
            "Envoie maintenant le fichier depuis la 📎 pièce jointe Telegram (PDF, JPG, JPEG ou PNG).\n"
            "Le fichier sera copié intégralement dans <code>data.json</code> et pourra être récupéré plus tard directement dans Telegram.",
            parse_mode="HTML", reply_markup=compta_menu_keyboard()
        )
    elif action in {"add_invoice","add_credit","add_payment","add_expense","add_bank"}:
        context.user_data["compta_pending"] = action
        await q.edit_message_text(
            f"✏️ Saisie : <b>{COMPTA_UI_ACTIONS.get(action.replace('add_',''), action)}</b>\n\n"
            "Pour éviter d'interférer avec tes autres formulaires, cette action est préparée ici "
            "et peut être branchée sur le système de conversation existant.",
            reply_markup=compta_menu_keyboard(), parse_mode="HTML")
    elif action == "reconcile":
        unmatched = compta_reconcile(db)
        await q.edit_message_text(
            f"🏦 <b>RAPPROCHEMENT</b>\n\n"
            f"Non rapprochées : {len(unmatched)}",
            reply_markup=compta_section_keyboard("bank"), parse_mode="HTML")
    return True

# ============================================================
# 📎 PIÈCES JOINTES — PDF / JPG / JPEG / PNG — STOCKAGE JSON + RÉCUPÉRATION
# ============================================================
COMPTA_ALLOWED_ATTACHMENTS = {".pdf", ".jpg", ".jpeg", ".png"}
COMPTA_MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # évite de rendre data.json énorme


def _compta_record_collections():
    return {
        "invoice": "invoices",
        "credit": "credit_notes",
        "payment": "payments",
        "expense": "expenses",
        "bank": "bank_lines",
    }


def _compta_attachment_list(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Migre l'ancien champ attachment vers attachments sans perdre les données."""
    items = record.get("attachments")
    if not isinstance(items, list):
        items = []
    old = record.get("attachment")
    if isinstance(old, dict) and old not in items:
        items.append(old)
        record["attachments"] = items
    record.setdefault("attachments", items)
    return items


def compta_next_attachment_id(db) -> str:
    c = compta_data(db)
    n = int(c.get("next_attachment", 1))
    c["next_attachment"] = n + 1
    return f"PJ-{n:06d}"


def compta_attachment_to_json(file_bytes, file_name, mime_type="", attachment_id=""):
    ext = Path(str(file_name)).suffix.lower()
    if ext not in COMPTA_ALLOWED_ATTACHMENTS:
        raise ValueError("Format non pris en charge. Utilise PDF, JPG, JPEG ou PNG.")
    if len(file_bytes) > COMPTA_MAX_ATTACHMENT_BYTES:
        raise ValueError("Fichier trop volumineux. Limite : 15 Mo.")
    return {
        "id": str(attachment_id),
        "file_name": str(file_name),
        "mime_type": str(mime_type or "application/octet-stream"),
        "size": len(file_bytes),
        "encoding": "base64",
        "data_base64": base64.b64encode(file_bytes).decode("ascii"),
        "created_at": now_iso(),
    }


def compta_attachment_from_json(attachment):
    if not attachment or attachment.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(attachment.get("data_base64", ""), validate=True)
    except (ValueError, TypeError):
        return None


def compta_store_attachment(db, kind, record, file_bytes, file_name, mime_type=""):
    """Stocke le fichier complet en Base64 dans le JSON, lié à l'enregistrement."""
    aid = compta_next_attachment_id(db)
    attachment = compta_attachment_to_json(file_bytes, file_name, mime_type, aid)
    attachment["record_kind"] = kind
    attachment["record_number"] = str(record.get("number") or record.get("id") or record.get("date") or "")
    _compta_attachment_list(record).append(attachment)
    # On garde aussi une entrée d'index légère pour les recherches globales.
    compta_data(db).setdefault("documents", [])
    compta_data(db)["documents"] = [
        x for x in compta_data(db)["documents"] if x.get("id") != aid
    ]
    compta_data(db)["documents"].append({
        "id": aid,
        "type": kind,
        "record_number": attachment["record_number"],
        "file_name": attachment["file_name"],
        "mime_type": attachment["mime_type"],
        "size": attachment["size"],
        "created_at": attachment["created_at"],
    })
    return attachment


def compta_store_document(db, file_bytes, file_name, mime_type, doc_type, label=""):
    """Stocke un document global (ex. FEC) directement dans comptabilite.documents."""
    aid = compta_next_attachment_id(db)
    if len(file_bytes) > COMPTA_MAX_ATTACHMENT_BYTES:
        raise ValueError("Document trop volumineux.")
    attachment = {
        "id": aid,
        "file_name": str(file_name),
        "mime_type": str(mime_type or "application/octet-stream"),
        "size": len(file_bytes),
        "encoding": "base64",
        "data_base64": base64.b64encode(file_bytes).decode("ascii"),
        "record_kind": doc_type,
        "record_number": "",
        "label": label,
        "created_at": now_iso(),
    }
    compta_data(db).setdefault("documents", []).append(attachment)
    return attachment


def _compta_iter_attachments(db):
    c = compta_data(db)
    for kind, key in _compta_record_collections().items():
        for record in c.get(key, []):
            for att in _compta_attachment_list(record):
                if att.get("id"):
                    yield att
    for att in c.get("documents", []):
        if att.get("id"):
            yield att


def compta_find_attachment(db, attachment_id):
    return next((a for a in _compta_iter_attachments(db) if str(a.get("id")) == str(attachment_id)), None)


def compta_invoice_attachment(db, invoice_number):
    c = compta_data(db)
    invoice = next((x for x in c["invoices"] if x.get("number") == invoice_number), None)
    return None if invoice is None else _compta_attachment_list(invoice)


async def compta_send_attachment(message, db, attachment_id):
    attachment = compta_find_attachment(db, attachment_id)
    if not attachment:
        await message.reply_text("❌ Pièce jointe introuvable.")
        return False
    data = compta_attachment_from_json(attachment)
    if data is None:
        await message.reply_text("❌ Impossible de reconstruire ce fichier depuis data.json.")
        return False
    filename = attachment.get("file_name", "piece_jointe")
    mime = attachment.get("mime_type", "")
    # À la récupération, le fichier est envoyé DIRECTEMENT dans le chat Telegram.
    # Les photos restent affichées comme photos ; les PDF/autres fichiers restent
    # des documents téléchargeables avec leur nom d'origine.
    caption = f"📎 {filename}\n🆔 {attachment.get('id')}"
    if mime.startswith("image/") and Path(filename).suffix.lower() in {".jpg", ".jpeg", ".png"}:
        await message.reply_photo(
            photo=io.BytesIO(data),
            caption=caption,
        )
    else:
        await message.reply_document(
            document=io.BytesIO(data),
            filename=filename,
            caption=caption,
        )
    return True


async def compta_show_all_attachments(q, db):
    items = list(_compta_iter_attachments(db))
    if not items:
        await q.edit_message_text(
            "📎 <b>PIÈCES JOINTES</b>\n\nAucun PDF, JPG, JPEG ou PNG enregistré.",
            reply_markup=compta_menu_keyboard(), parse_mode="HTML"
        )
        return
    buttons = []
    lines = ["📎 <b>PIÈCES JOINTES ENREGISTRÉES</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for att in items[-30:]:
        size_mb = float(att.get("size", 0)) / (1024 * 1024)
        label = att.get("label") or att.get("record_number") or att.get("record_kind") or "Document"
        lines.append(f"\n📄 <b>{esc(att.get('file_name'))}</b>\n{esc(label)} • {size_mb:.2f} Mo • <code>{esc(att.get('id'))}</code>")
        buttons.append([InlineKeyboardButton(
            f"📤 Envoyer dans Telegram : {str(att.get('file_name',''))[:28]}",
            callback_data=f"compta:download_attachment:{att.get('id')}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Retour", callback_data="compta:menu")])
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")


async def compta_show_attachments_by_kind(q, db, kind):
    items = [
        a for a in _compta_iter_attachments(db)
        if str(a.get("record_kind", "")).lower() == kind
    ]
    back = {"invoice":"invoices","credit":"credits","payment":"payments","expense":"expenses","bank":"bank"}.get(kind, "menu")
    if not items:
        await q.edit_message_text(
            f"📎 <b>FICHIERS {esc(kind.upper())}</b>\n\nAucun fichier enregistré.",
            reply_markup=compta_section_keyboard(back),
            parse_mode="HTML"
        )
        return

    buttons = []
    lines = [f"📎 <b>FICHIERS {esc(kind.upper())}</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for att in items[-30:]:
        lines.append(
            f"\n📄 <b>{esc(att.get('file_name','document'))}</b> "
            f"• <code>{esc(att.get('id',''))}</code>"
        )
        buttons.append([InlineKeyboardButton(
            f"📤 Envoyer dans Telegram : {str(att.get('file_name','document'))[:28]}",
            callback_data=f"compta:download_attachment:{att.get('id')}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Retour", callback_data=f"compta:{back}")])
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )


async def compta_choose_attachment_target(q, context, db, kind):
    c = compta_data(db)
    key = _compta_record_collections().get(kind)
    if not key:
        await q.answer("Section non prise en charge.", show_alert=True)
        return
    records = c.get(key, [])
    if not records:
        await q.edit_message_text(
            f"📎 Aucun élément dans cette section pour le moment.",
            reply_markup=compta_section_keyboard(kind), parse_mode="HTML"
        )
        return
    buttons = []
    for rec in records[-30:]:
        ident = rec.get("number") or rec.get("id") or rec.get("date") or "élément"
        label = rec.get("customer") or rec.get("label") or rec.get("method") or rec.get("date") or ""
        target = f"{kind}|{ident}"
        buttons.append([InlineKeyboardButton(
            f"📎 {str(ident)[:25]} — {str(label)[:22]}",
            callback_data=f"compta:attach_to:{target}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Retour", callback_data=f"compta:{kind}")])
    await q.edit_message_text(
        f"📎 <b>CHOISIR L'ÉLÉMENT</b>\n\nSection : <b>{esc(kind)}</b>\n\nChoisis l'élément auquel tu veux joindre le PDF/photo.",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
    )


async def compta_receive_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update):
        return True
    target = context.user_data.get("compta_attachment_target")
    if not target:
        return False

    message = update.effective_message
    file_name = ""
    mime_type = ""
    file_obj = None
    try:
        if message.document:
            file_name = message.document.file_name or "document.pdf"
            mime_type = message.document.mime_type or ""
            file_obj = message.document
        elif message.photo:
            file_name = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            mime_type = "image/jpeg"
            file_obj = message.photo[-1]
        else:
            return False

        ext = Path(file_name).suffix.lower()
        if ext not in COMPTA_ALLOWED_ATTACHMENTS:
            await message.reply_text("❌ Format refusé. Envoie uniquement PDF, JPG, JPEG ou PNG.")
            return True

        tg_file = await file_obj.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        if len(data) > COMPTA_MAX_ATTACHMENT_BYTES:
            await message.reply_text("❌ Fichier trop volumineux. Limite : 15 Mo.")
            return True

        kind, ident = target.split("|", 1)

        if kind == "document":
            attachment = compta_store_document(
                DB, data, file_name, mime_type,
                doc_type=ident,
                label=f"Pièce jointe {ident}"
            )
        else:
            key = _compta_record_collections().get(kind)
            records = compta_data(DB).get(key, []) if key else []
            record = next((r for r in records if str(r.get("number") or r.get("id") or r.get("date") or "") == ident), None)
            if not record:
                await message.reply_text("❌ Élément comptable introuvable. Recommence depuis le menu Pièces jointes.")
                context.user_data.pop("compta_attachment_target", None)
                return True
            attachment = compta_store_attachment(DB, kind, record, data, file_name, mime_type)

        save_db(DB)
        context.user_data.pop("compta_attachment_target", None)
        await message.reply_text(
            "✅ <b>Pièce jointe enregistrée</b>\n\n"
            f"📄 {esc(file_name)}\n"
            f"📦 Taille : {len(data)/(1024*1024):.2f} Mo\n"
            f"🆔 <code>{esc(attachment['id'])}</code>\n\n"
            "💾 Le fichier complet est enregistré dans <code>data.json</code>.\n"
            "📤 Tu peux le récupérer à tout moment : il sera renvoyé directement dans ce chat Telegram, avec son format d’origine.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📤 Envoyer ce fichier dans Telegram",
                    callback_data=f"compta:download_attachment:{attachment['id']}"
                )],
                [InlineKeyboardButton("📎 Voir tous les fichiers", callback_data="compta:attachments")],
                [InlineKeyboardButton("⬅️ Comptabilité", callback_data="compta:menu")],
            ]),
        )
        return True
    except Exception as exc:
        log.exception("Erreur pièce jointe")
        await message.reply_text(f"❌ Impossible d'enregistrer le fichier : {esc(exc)}")
        return True


# ============================================================
# DÉMARRAGE — toujours après la définition complète du module
# ============================================================
if __name__ == "__main__":
    main()
