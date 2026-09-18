import os
import json
import html
import logging
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
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


def home_text() -> str:
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
        f"🏢 Fournisseurs : <b>{len(DB['fournisseurs'])}</b>\n"
        f"💰 Valeur d'achat du stock : <b>{money(value)}</b>\n\n"
        "🟢 <i>Base prête pour tes vraies données.</i>"
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
        home_text(), parse_mode=ParseMode.HTML, reply_markup=menu()
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
            home_text(), parse_mode=ParseMode.HTML, reply_markup=menu()
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
            f"💰 Valeur achat : <b>{money(value)}</b>\n"
            f"📋 Commandes : <b>{len(DB['commandes'])}</b>\n"
            f"🚚 Livraisons reçues : <b>{delivered}</b>\n"
            f"🔧 Réparations actives : <b>{repair_open}</b>\n"
            f"🏢 Fournisseurs : <b>{len(DB['fournisseurs'])}</b>\n"
            f"👥 Utilisateurs : <b>{len(DB['users'])}</b>\n"
            f"📝 Événements journalisés : <b>{len(DB['activity_log'])}</b>"
        )
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
        return

    if action == "users":
        if not admin(update.effective_chat.id):
            await q.answer("Administrateur uniquement", show_alert=True)
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
        if not admin(update.effective_chat.id):
            await q.answer("Administrateur uniquement", show_alert=True)
            return

        target = action.split(":", 1)[1]
        rec = DB.get("users", {}).get(target)

        if not rec:
            await q.answer("Collaborateur introuvable.", show_alert=True)
            return

        if rec.get("role") == "admin" or target == str(ADMIN_CHAT_ID):
            await q.answer("Impossible de révoquer l'administrateur.", show_alert=True)
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
        if not admin(update.effective_chat.id):
            await q.answer("Administrateur uniquement", show_alert=True)
            return

        target = action.split(":", 1)[1]
        rec = DB.get("users", {}).get(target)

        if not rec:
            await q.answer("Déjà révoqué.", show_alert=True)
            return

        if rec.get("role") == "admin" or target == str(ADMIN_CHAT_ID):
            await q.answer("Impossible de révoquer l'administrateur.", show_alert=True)
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
# V2 — SOUS-MENUS RÉELS + DOSSIERS DÉBLOCAGE
# ============================================================

V2_SECTIONS = {
 "stock":("📦 STOCK",[("➕ Ajouter","v2act:stock:add"),("📋 Voir","v2act:stock:list"),("🔎 Rechercher","v2act:stock:search"),("✏️ Modifier","v2act:stock:edit"),("🗑️ Supprimer","v2act:stock:delete")]),
 "ruptures":("🚨 RUPTURES",[("📋 Voir","v2act:ruptures:list"),("🔎 Rechercher","v2act:ruptures:search")]),
 "commandes":("📋 COMMANDES",[("➕ Ajouter","v2act:commandes:add"),("📋 Voir","v2act:commandes:list"),("🔎 Rechercher","v2act:commandes:search"),("✏️ Modifier","v2act:commandes:edit"),("🗑️ Supprimer","v2act:commandes:delete")]),
 "livraisons":("🚚 LIVRAISONS",[("➕ Ajouter","v2act:livraisons:add"),("📋 Voir","v2act:livraisons:list"),("🔎 Rechercher","v2act:livraisons:search"),("✏️ Modifier","v2act:livraisons:edit"),("🗑️ Supprimer","v2act:livraisons:delete")]),
 "reparations":("🔧 RÉPARATIONS",[("➕ Ajouter une réparation","v2act:reparations:add"),("📋 Voir","v2act:reparations:list"),("🔎 Rechercher","v2act:reparations:search"),("✏️ Modifier","v2act:reparations:edit"),("🗑️ Supprimer","v2act:reparations:delete"),("⏸️ Pause","v2act:reparations:pause"),("▶️ Reprendre","v2act:reparations:resume"),("📦 Attente pièce","v2act:reparations:parts"),("👤 Attente client","v2act:reparations:customer"),("🧪 À tester","v2act:reparations:test"),("✅ Terminer","v2act:reparations:done"),("📦 Livrée","v2act:reparations:delivered"),("❌ Annuler","v2act:reparations:cancel")]),
 "deblocages":("🔓 DÉBLOCAGES",[("➕ Nouveau dossier","v2act:deblocages:add"),("📋 Voir","v2act:deblocages:list"),("🔎 Rechercher","v2act:deblocages:search")]),
 "fournisseurs":("🏢 FOURNISSEURS",[("➕ Ajouter","v2act:fournisseurs:add"),("📋 Voir","v2act:fournisseurs:list"),("🔎 Rechercher","v2act:fournisseurs:search"),("✏️ Modifier","v2act:fournisseurs:edit"),("🗑️ Supprimer","v2act:fournisseurs:delete")]),
 "mouvements":("📥📤 MOUVEMENTS",[("📥 Entrée","v2act:mouvements:in"),("📤 Sortie","v2act:mouvements:out"),("📋 Historique","v2act:mouvements:list")]),
 "collaborateurs":("👥 COLLABORATEURS",[("➕ Ajouter","v2act:collaborateurs:add"),("📋 Voir","v2act:collaborateurs:list"),("🗑️ Révoquer","v2act:collaborateurs:delete")]),
}
V2_STATUSES=["EN ATTENTE","EN COURS","EN PAUSE","EN ATTENTE PIÈCE","EN ATTENTE CLIENT","À TESTER","TERMINÉE","LIVRÉE","ANNULÉE"]

def v2_keyboard(section):
    return InlineKeyboardMarkup([[InlineKeyboardButton(a,callback_data=d)] for a,d in V2_SECTIONS[section][1]]+[[InlineKeyboardButton("⬅️ Retour",callback_data="home")]])

def v2_text(section,qry=None):
    key=section
    items=DB.get(key,[])
    if section=="ruptures":
        items=[x for x in DB.get("stock",[]) if int(x.get("quantite",0))<=int(x.get("seuil",2))]
    if qry:
        q=qry.lower(); items=[x for x in items if q in " ".join(str(v) for v in x.values()).lower()]
    title=V2_SECTIONS[section][0]
    if not items:return title+"\n━━━━━━━━━━━━━━━━━━━━\n\nAucun élément."
    lines=[title,"━━━━━━━━━━━━━━━━━━━━"]
    for x in items[:40]:
        lines.append("\n"+esc(str(x)[:900]))
    return "\n".join(lines)

async def v2_handler(update,context):
    q=update.callback_query
    if not q or not await require_access(update): return
    await q.answer(); data=q.data or ""
    if data.startswith("v2menu:"):
        sec=data.split(":",1)[1]
        if sec in V2_SECTIONS:
            await q.edit_message_text(V2_SECTIONS[sec][0]+"\n\nChoisis une action :",reply_markup=v2_keyboard(sec))
        return
    if not data.startswith("v2act:"): return
    _,sec,act=data.split(":",2)
    if sec not in V2_SECTIONS:return
    if sec=="deblocages" and act=="add":
        DB.setdefault("deblocages",[])
        context.user_data.clear();context.user_data["v2_flow"]="deblocage";context.user_data["v2_step"]=1;context.user_data["v2_form"]={}
        await q.edit_message_text("🔓 <b>NOUVEAU DOSSIER</b>\n\nFRP / Google ou iCloud / Apple ? Réponds <b>FRP</b> ou <b>iCloud</b>.",parse_mode=ParseMode.HTML,reply_markup=back_menu());return
    if sec=="reparations" and act=="add":
        context.user_data.clear();context.user_data["v2_flow"]="reparation";context.user_data["v2_step"]=1;context.user_data["v2_form"]={}
        await q.edit_message_text("➕ <b>NOUVELLE RÉPARATION</b>\n\n1/6 — Numéro de réparation ?",parse_mode=ParseMode.HTML,reply_markup=back_menu());return
    if act=="list":
        await q.edit_message_text(v2_text(sec),reply_markup=v2_keyboard(sec),parse_mode=ParseMode.HTML);return
    if act=="search":
        context.user_data["v2_search"]=sec
        await q.edit_message_text("🔎 Envoie le terme à rechercher.",reply_markup=back_menu());return
    if sec=="reparations" and act in {"pause","resume","parts","customer","test","done","delivered","cancel"}:
        st={"pause":"EN PAUSE","resume":"EN COURS","parts":"EN ATTENTE PIÈCE","customer":"EN ATTENTE CLIENT","test":"À TESTER","done":"TERMINÉE","delivered":"LIVRÉE","cancel":"ANNULÉE"}[act]
        context.user_data["v2_status"]=st
        await q.edit_message_text(f"📌 Nouveau statut : <b>{st}</b>\n\nEnvoie le numéro ou l'IMEI de la réparation.",parse_mode=ParseMode.HTML,reply_markup=back_menu());return
    await q.edit_message_text("ℹ️ Action prête. Pour préserver les fonctions existantes, la modification/suppression passe d'abord par une recherche.",reply_markup=v2_keyboard(sec))

async def v2_text_router(update,context):
    if not update.effective_message or not authorized(update.effective_chat.id): return False
    txt=update.effective_message.text.strip()
    if context.user_data.get("v2_search"):
        sec=context.user_data.pop("v2_search")
        await update.effective_message.reply_text(v2_text(sec,txt),reply_markup=v2_keyboard(sec),parse_mode=ParseMode.HTML);return True
    if context.user_data.get("v2_status"):
        st=context.user_data.pop("v2_status");needle=txt.lower()
        for x in DB.get("reparations",[]):
            if needle in " ".join(str(v) for v in x.values()).lower():
                old=x.get("statut","");x["statut"]=st;x.setdefault("historique",[]).append({"date":now_iso(),"action":"statut","ancien":old,"nouveau":st,"user":str(update.effective_chat.id)});save_db(DB)
                await update.effective_message.reply_text(f"✅ {esc(old)} → <b>{esc(st)}</b>",parse_mode=ParseMode.HTML,reply_markup=menu());return True
        await update.effective_message.reply_text("❌ Réparation introuvable.",reply_markup=menu());return True
    flow=context.user_data.get("v2_flow")
    if flow=="reparation":
        f=context.user_data.setdefault("v2_form",{});step=context.user_data.get("v2_step",1)
        keys=[("numero","2/6 — Modèle/appareil ?"),("appareil","3/6 — Client ?"),("client","4/6 — Panne ?"),("panne","5/6 — IMEI (ou -) ?"),("imei","6/6 — Statut ?")]
        if step<=5:
            k,p=keys[step-1];f[k]=txt;context.user_data["v2_step"]=step+1;await update.effective_message.reply_text(p,reply_markup=back_menu());return True
        st=txt.upper()
        if st not in V2_STATUSES:
            await update.effective_message.reply_text("❌ Statut invalide.",reply_markup=back_menu());return True
        item={"numero":f["numero"],"appareil":f["appareil"],"client":f["client"],"panne":f["panne"],"identifiant":"" if f["imei"]=="-" else f["imei"],"type_identifiant":"IMEI" if f["imei"]!="-" else "","statut":st,"date":now_iso(),"historique":[]}
        DB.setdefault("reparations",[]).append(item);save_db(DB);context.user_data.clear()
        await update.effective_message.reply_text("✅ <b>Réparation enregistrée.</b>",parse_mode=ParseMode.HTML,reply_markup=menu());return True
    if flow=="deblocage":
        f=context.user_data.setdefault("v2_form",{});step=context.user_data.get("v2_step",1)
        if step==1:
            if txt.upper() not in {"FRP","ICLOUD","I-CLOUD"}: await update.effective_message.reply_text("Réponds FRP ou iCloud.",reply_markup=back_menu());return True
            f["type"]="FRP / Google" if txt.upper()=="FRP" else "iCloud / Apple";p="2/4 — Modèle/appareil ?"
        elif step==2:f["appareil"]=txt;p="3/4 — IMEI (ou -) ?"
        elif step==3:f["imei"]="" if txt=="-" else txt;p="4/4 — Client ?"
        else:
            f["client"]=txt;f["statut"]="EN ATTENTE";f["date"]=now_iso();f["numero"]=f"DB-{len(DB.get('deblocages',[]))+1:05d}";DB.setdefault("deblocages",[]).append(f.copy());save_db(DB);context.user_data.clear();await update.effective_message.reply_text(f"✅ <b>Dossier {esc(f['numero'])} enregistré.</b>",parse_mode=ParseMode.HTML,reply_markup=menu());return True
        context.user_data["v2_step"]=step+1;await update.effective_message.reply_text(p,reply_markup=back_menu());return True
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
