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
SEARCH, ADD_STOCK, ADD_SUPPLIER, ADD_ORDER, ADD_DELIVERY, ADD_REPAIR, ADD_GENERIC, EDIT_GENERIC = range(8)


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
    """Menu principal : chaque module ouvre un sous-menu dédié."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Stock", callback_data="menu:stock"),
            InlineKeyboardButton("🚨 Ruptures", callback_data="menu:ruptures"),
        ],
        [
            InlineKeyboardButton("📋 Commandes", callback_data="menu:commandes"),
            InlineKeyboardButton("🚚 Livraisons", callback_data="menu:livraisons"),
        ],
        [
            InlineKeyboardButton("🔧 Réparations", callback_data="menu:reparations"),
            InlineKeyboardButton("🔎 Rechercher", callback_data="search"),
        ],
        [
            InlineKeyboardButton("🏢 Fournisseurs", callback_data="menu:fournisseurs"),
            InlineKeyboardButton("📊 Statistiques", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("📥📤 Mouvements", callback_data="menu:movement"),
            InlineKeyboardButton("📷 Scanner appareil", callback_data="scan_device"),
        ],
        [
            InlineKeyboardButton("👥 Collaborateurs", callback_data="users"),
            InlineKeyboardButton("📝 Activité", callback_data="activity"),
        ],
        [InlineKeyboardButton("🔄 Actualiser", callback_data="home")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Retour", callback_data="home")]
    ])


def module_menu(module: str) -> InlineKeyboardMarkup:
    """Sous-menu commun pour les modules de gestion."""
    labels = {
        "stock": ("📦 STOCK", "add_stock", "stock"),
        "ruptures": ("🚨 RUPTURES", "add_stock", "ruptures"),
        "commandes": ("📋 COMMANDES", "add:commandes", "commandes"),
        "livraisons": ("🚚 LIVRAISONS", "add:livraisons", "livraisons"),
        "reparations": ("🔧 RÉPARATIONS", "add:reparations", "reparations"),
        "fournisseurs": ("🏢 FOURNISSEURS", "add_supplier", "fournisseurs"),
    }
    title, add_action, view_action = labels[module]
    buttons = [
        [InlineKeyboardButton("➕ Ajouter", callback_data=add_action)],
        [InlineKeyboardButton("📋 Voir", callback_data=view_action)],
        [InlineKeyboardButton("🔎 Rechercher", callback_data=f"search:{module}")],
        [InlineKeyboardButton("✏️ Modifier", callback_data=f"edit:{module}")],
        [InlineKeyboardButton("🗑️ Supprimer", callback_data=f"delete:{module}")],
    ]
    if module == "ruptures":
        buttons = [
            [InlineKeyboardButton("📋 Voir les ruptures", callback_data="ruptures")],
            [InlineKeyboardButton("➕ Ajouter au stock", callback_data="add_stock")],
            [InlineKeyboardButton("🔎 Rechercher", callback_data="search:ruptures")],
        ]
    buttons.append([InlineKeyboardButton("⬅️ Retour", callback_data="home")])
    return InlineKeyboardMarkup(buttons)


def movement_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Entrée", callback_data="movement:in"),
            InlineKeyboardButton("📤 Sortie", callback_data="movement:out"),
        ],
        [InlineKeyboardButton("📋 Historique", callback_data="movements:list")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="home")],
    ])


def item_actions(module: str, item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Modifier", callback_data=f"edititem:{module}:{item_id}"),
            InlineKeyboardButton("🗑️ Supprimer", callback_data=f"deleteitem:{module}:{item_id}"),
        ],
        [InlineKeyboardButton("⬅️ Retour", callback_data=f"menu:{module}")],
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

    # Sous-menus des modules.
    if action.startswith("menu:"):
        module = action.split(":", 1)[1]
        if module == "movement":
            await q.edit_message_text(
                "📥📤 <b>MOUVEMENTS DE STOCK</b>\n\nChoisis une action :",
                parse_mode=ParseMode.HTML,
                reply_markup=movement_menu(),
            )
        elif module in {"stock", "ruptures", "commandes", "livraisons", "reparations", "fournisseurs"}:
            await q.edit_message_text(
                f"{_module_title(module)}\n\nChoisis une action :",
                parse_mode=ParseMode.HTML,
                reply_markup=module_menu(module),
            )
        return

    # Lancement des ajouts des modules qui utilisent le formulaire générique.
    if action.startswith("search:"):
        module = action.split(":", 1)[1]
        context.user_data["awaiting_search"] = True
        context.user_data["search_module"] = module
        await q.edit_message_text(
            f"🔎 <b>RECHERCHE {_module_title(module)}</b>\n\n"
            "Envoie le texte à rechercher.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return

    if action.startswith("add:"):
        module = action.split(":", 1)[1]
        if module in MODULE_FIELDS:
            return await start_generic_add(update, context, module)

    # Sélection d'un élément pour modification/suppression.
    if action.startswith("edit:"):
        module = action.split(":", 1)[1]
        if module in MODULE_FIELDS or module == "stock":
            await select_for_edit_delete(update, module, "edit")
        return

    if action.startswith("delete:"):
        module = action.split(":", 1)[1]
        if module in MODULE_FIELDS or module == "stock":
            await select_for_edit_delete(update, module, "delete")
        return

    # Suppression avec confirmation.
    if action.startswith("deleteitem:"):
        _, module, item_id = action.split(":", 2)
        items = _module_collection(module)
        item = next((x for x in items if str(x.get("id")) == item_id), None)
        if not item:
            await q.answer("Élément introuvable.", show_alert=True)
            return
        await q.edit_message_text(
            "⚠️ <b>CONFIRMER LA SUPPRESSION</b>\n\n"
            f"🆔 <code>{esc(item_id)}</code>\n\nCette action est définitive.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Supprimer", callback_data=f"confirmdelete:{module}:{item_id}"),
                    InlineKeyboardButton("❌ Annuler", callback_data=f"menu:{module}"),
                ]
            ]),
        )
        return

    if action.startswith("confirmdelete:"):
        _, module, item_id = action.split(":", 2)
        items = _module_collection(module)
        old_len = len(items)
        DB[module] = [x for x in items if str(x.get("id")) != item_id]
        if len(DB[module]) == old_len:
            await q.answer("Élément introuvable.", show_alert=True)
            return
        log_activity(update.effective_chat.id, f"{module.upper()}_DELETE", item_id)
        save_db(DB, make_backup=False)
        await q.edit_message_text(
            f"🗑️ <b>{_module_title(module)}</b>\n\n"
            f"Élément <code>{esc(item_id)}</code> supprimé.",
            parse_mode=ParseMode.HTML,
            reply_markup=module_menu(module),
        )
        return

    # Historique des mouvements.
    if action == "movements:list":
        lines = ["📥📤 <b>HISTORIQUE DES MOUVEMENTS</b>", "━━━━━━━━━━━━━━━━━━━━"]
        if not DB.get("mouvements"):
            lines.append("\nAucun mouvement.")
        for x in reversed(DB.get("mouvements", [])[-40:]):
            lines.append(
                f"\n🕒 {esc(x.get('date','-'))}\n"
                f"🔖 {esc(x.get('reference','-'))} • "
                f"{esc(x.get('type','-'))} • Qté {esc(x.get('quantite','-'))}"
            )
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=movement_menu()
        )
        return

    if action == "movement:in" or action == "movement:out":
        direction = "IN" if action.endswith(":in") else "OUT"
        context.user_data["awaiting_movement"] = direction
        await q.edit_message_text(
            f"{'📥 ENTRÉE' if direction == 'IN' else '📤 SORTIE'} DE STOCK\n\n"
            "Envoie : <code>REF QUANTITE</code>\n"
            "Exemple : <code>IP15PRO-BLK-256 2</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return

    if action.startswith("edititem:"):
        _, module, item_id = action.split(":", 2)
        items = _module_collection(module)
        item = next((x for x in items if str(x.get("id")) == item_id), None)
        if not item:
            await q.answer("Élément introuvable.", show_alert=True)
            return
        fields = MODULE_FIELDS.get(module, [])
        if module == "stock":
            fields = [
                ("produit", "📦 Produit"),
                ("reference", "🔖 Référence"),
                ("quantite", "📊 Quantité"),
                ("prix_achat", "💶 Prix d'achat"),
                ("seuil", "🚨 Seuil"),
                ("emplacement", "📍 Emplacement"),
                ("fournisseur", "🏢 Fournisseur"),
            ]
        buttons = [
            [InlineKeyboardButton(label, callback_data=f"editfield:{module}:{item_id}:{key}")]
            for key, label in fields
        ]
        buttons.append([InlineKeyboardButton("⬅️ Retour", callback_data=f"menu:{module}")])
        await q.edit_message_text(
            f"✏️ <b>MODIFIER {esc(item_id)}</b>\n\nChoisis le champ à modifier :",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action.startswith("editfield:"):
        _, module, item_id, field = action.split(":", 3)
        context.user_data["edit_module"] = module
        context.user_data["edit_item_id"] = item_id
        context.user_data["edit_field"] = field
        labels = dict(MODULE_FIELDS.get(module, []))
        if module == "stock":
            labels.update(dict([
                ("produit", "📦 Produit"), ("reference", "🔖 Référence"),
                ("quantite", "📊 Quantité"), ("prix_achat", "💶 Prix d'achat"),
                ("seuil", "🚨 Seuil"), ("emplacement", "📍 Emplacement"),
                ("fournisseur", "🏢 Fournisseur")
            ]))
        await q.edit_message_text(
            f"✏️ {labels.get(field, field)}\n\nEnvoie la nouvelle valeur.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return EDIT_GENERIC

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
    if not context.user_data.get("awaiting_search") and context.user_data.get("awaiting_movement"):
        direction = context.user_data.pop("awaiting_movement")
        parts = update.effective_message.text.strip().split()
        if len(parts) != 2:
            await update.effective_message.reply_text(
                "❌ Format invalide. Utilise : REF QUANTITE",
                reply_markup=movement_menu(),
            )
            return
        context.args = parts
        await stock_movement(update, context, direction)
        return

    if not context.user_data.get("awaiting_search"):
        return

    query = update.effective_message.text.strip().lower()
    context.user_data["awaiting_search"] = False
    context.user_data.pop("search_module", None)

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
        save_db(DB, make_backup=False)
        context.user_data.clear()
        await update.effective_message.reply_text(
            "✅ Fournisseur ajouté.", reply_markup=menu()
        )
        return ConversationHandler.END
    return ConversationHandler.END



MODULE_FIELDS = {
    "commandes": [
        ("numero", "🔢 Numéro de commande"),
        ("fournisseur", "🏢 Fournisseur"),
        ("statut", "📌 Statut"),
        ("date", "📅 Date"),
        ("montant", "💶 Montant"),
    ],
    "livraisons": [
        ("commande", "📦 Numéro de commande"),
        ("transporteur", "🚛 Transporteur"),
        ("statut", "📌 Statut"),
        ("suivi", "🔎 Numéro de suivi"),
        ("date_prevue", "📅 Date prévue"),
    ],
    "reparations": [
        ("numero", "🔢 Numéro de réparation"),
        ("appareil", "📱 Appareil / modèle"),
        ("client", "👤 Client"),
        ("panne", "🛠️ Panne / réparation effectuée"),
        ("statut", "📌 Statut"),
        ("devis", "💶 Prix / devis"),
        ("notes", "📝 Notes"),
    ],
}


def _module_collection(module: str):
    return DB.get(module, [])


def _module_title(module: str) -> str:
    return {
        "commandes": "📋 COMMANDES",
        "livraisons": "🚚 LIVRAISONS",
        "reparations": "🔧 RÉPARATIONS",
        "fournisseurs": "🏢 FOURNISSEURS",
        "stock": "📦 STOCK",
    }.get(module, module.upper())


def _item_id(module: str, item: dict[str, Any]) -> str:
    return str(item.get("id", ""))


async def start_generic_add(update: Update, context: ContextTypes.DEFAULT_TYPE, module: str):
    if not await require_access(update):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["generic_module"] = module
    context.user_data["generic_index"] = 0
    fields = MODULE_FIELDS[module]
    context.user_data["generic_step"] = fields[0][0]
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        f"➕ <b>{_module_title(module)}</b>\n\n{fields[0][1]}\n"
        "Tu peux utiliser /cancel pour annuler.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu(),
    )
    return ADD_GENERIC


async def generic_add_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return ConversationHandler.END

    module = context.user_data.get("generic_module")
    if module not in MODULE_FIELDS:
        return ConversationHandler.END

    text = update.effective_message.text.strip()
    fields = MODULE_FIELDS[module]
    index = int(context.user_data.get("generic_index", 0))
    key, _ = fields[index]
    context.user_data[key] = text
    index += 1

    if index < len(fields):
        context.user_data["generic_index"] = index
        context.user_data["generic_step"] = fields[index][0]
        await update.effective_message.reply_text(
            fields[index][1],
            reply_markup=back_menu(),
        )
        return ADD_GENERIC

    data = {k: context.user_data.get(k, "") for k, _ in fields}
    data["id"] = f"{module[:3].upper()}-{len(DB[module])+1:05d}"
    data["created_at"] = now_iso()
    data["updated_at"] = now_iso()
    data["chat_id"] = update.effective_chat.id

    # Normalise les montants numériques.
    for key in ("montant", "devis"):
        if key in data:
            try:
                data[key] = float(str(data[key]).replace(",", "."))
            except ValueError:
                await update.effective_message.reply_text(
                    f"❌ {key} doit être un nombre. Recommence avec /start."
                )
                context.user_data.clear()
                return ConversationHandler.END

    DB[module].append(data)
    log_activity(update.effective_chat.id, f"{module.upper()}_CREATE", data.get("id", ""))
    save_db(DB, make_backup=False)

    context.user_data.clear()
    await update.effective_message.reply_text(
        f"✅ <b>{_module_title(module)} ajouté</b>\n\n"
        f"🆔 <code>{esc(data['id'])}</code>\n"
        + "\n".join(
            f"{label.split(' ', 1)[0]} {esc(data.get(key, '-'))}"
            for key, label in fields
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=module_menu(module),
    )
    return ConversationHandler.END


def list_module_text(module: str) -> str:
    items = _module_collection(module)
    title = _module_title(module)
    if not items:
        return f"{title}\n━━━━━━━━━━━━━━━━━━━━\n\nAucun élément."
    lines = [title, "━━━━━━━━━━━━━━━━━━━━"]
    for x in items[:40]:
        lines.append(f"\n🆔 <code>{esc(x.get('id', '-'))}</code>")
        for key, label in MODULE_FIELDS.get(module, []):
            val = x.get(key, "-")
            if key in {"montant", "devis"}:
                val = money(val)
            lines.append(f"{label} : {esc(val)}")
    return "\n".join(lines)


async def select_for_edit_delete(update: Update, module: str, mode: str):
    if not await require_access(update):
        return
    q = update.callback_query
    items = _module_collection(module)
    if not items:
        await q.edit_message_text(
            f"{_module_title(module)}\n\nAucun élément à {mode}.",
            reply_markup=module_menu(module),
        )
        return
    buttons = []
    for x in items[:30]:
        label = str(x.get("id", ""))[:20]
        extra = x.get("produit") or x.get("appareil") or x.get("nom") or x.get("numero") or ""
        buttons.append([
            InlineKeyboardButton(
                f"{label} • {str(extra)[:22]}",
                callback_data=f"{mode}item:{module}:{x.get('id','')}",
            )
        ])
    buttons.append([InlineKeyboardButton("⬅️ Retour", callback_data=f"menu:{module}")])
    await q.edit_message_text(
        f"{'✏️' if mode == 'edit' else '🗑️'} <b>{_module_title(module)}</b>\n\nSélectionne un élément :",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )



async def edit_generic_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return ConversationHandler.END

    module = context.user_data.get("edit_module")
    item_id = context.user_data.get("edit_item_id")
    field = context.user_data.get("edit_field")
    items = _module_collection(module)
    item = next((x for x in items if str(x.get("id")) == str(item_id)), None)

    if not item:
        context.user_data.clear()
        await update.effective_message.reply_text("❌ Élément introuvable.", reply_markup=menu())
        return ConversationHandler.END

    value = update.effective_message.text.strip()
    if field in {"quantite", "seuil"}:
        try:
            value = int(value)
        except ValueError:
            await update.effective_message.reply_text("❌ Cette valeur doit être un entier.")
            return EDIT_GENERIC
    elif field in {"prix_achat", "montant", "devis"}:
        try:
            value = float(value.replace(",", "."))
        except ValueError:
            await update.effective_message.reply_text("❌ Cette valeur doit être un nombre.")
            return EDIT_GENERIC

    item[field] = value
    item["updated_at"] = now_iso()
    log_activity(update.effective_chat.id, f"{module.upper()}_EDIT", f"{item_id}: {field}")
    save_db(DB, make_backup=False)
    context.user_data.clear()

    await update.effective_message.reply_text(
        f"✅ <b>{_module_title(module)}</b> modifié.\\n\\n"
        f"🆔 <code>{esc(item_id)}</code>\\n"
        f"Champ : <b>{esc(field)}</b>\\n"
        f"Nouvelle valeur : <code>{esc(value)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=module_menu(module),
    )
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



async def start_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    if not await require_access(update):
        return ConversationHandler.END
    action = q.data
    _, module, item_id, field = action.split(":", 3)
    context.user_data["edit_module"] = module
    context.user_data["edit_item_id"] = item_id
    context.user_data["edit_field"] = field
    labels = dict(MODULE_FIELDS.get(module, []))
    if module == "stock":
        labels.update(dict([
            ("produit", "📦 Produit"), ("reference", "🔖 Référence"),
            ("quantite", "📊 Quantité"), ("prix_achat", "💶 Prix d'achat"),
            ("seuil", "🚨 Seuil"), ("emplacement", "📍 Emplacement"),
            ("fournisseur", "🏢 Fournisseur")
        ]))
    await q.answer()
    await q.edit_message_text(
        f"✏️ {labels.get(field, field)}\n\nEnvoie la nouvelle valeur.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu(),
    )
    return EDIT_GENERIC

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

    generic_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                lambda update, context: start_generic_add(update, context, "commandes"),
                pattern=r"^add:commandes$",
            ),
            CallbackQueryHandler(
                lambda update, context: start_generic_add(update, context, "livraisons"),
                pattern=r"^add:livraisons$",
            ),
            CallbackQueryHandler(
                lambda update, context: start_generic_add(update, context, "reparations"),
                pattern=r"^add:reparations$",
            ),
        ],
        states={
            ADD_GENERIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, generic_add_flow)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                start_edit_field,
                pattern=r"^editfield:",
            ),
        ],
        states={
            EDIT_GENERIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_generic_flow)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(generic_conv)
    app.add_handler(edit_conv)
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, scanner_webapp))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_message))

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
