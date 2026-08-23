import os
import json
import math
import io
import time
import secrets
from io import BytesIO

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput
import chat_exporter
import qrcode
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

SELLER_ROLE = int(os.getenv("SELLER_ROLE", "0"))
STAFF_ROLE = int(os.getenv("STAFF_ROLE", "0"))
CLIENT_ROLE = int(os.getenv("CLIENT_ROLE", "0"))

REQ_TICKET_CAT = int(os.getenv("REQ_TICKET_CAT", "0"))
SUPP_TICKET_CAT = int(os.getenv("SUPP_TICKET_CAT", "0"))
CLAIMED_CAT = int(os.getenv("CLAIMED_CAT", "0"))
CLOSED_CAT = int(os.getenv("CLOSED_CAT", "0"))

TRANSCRIPT_CHANNEL = int(os.getenv("TRANSCRIPT_CHANNEL", "0"))
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "0"))

FOOTER = os.getenv("FOOTER", "Sarah Shop | Shop Bot")

DATA_FILE = "data.json"

# ============================================================
# WEBSITE / PAYMENT API
# ============================================================

PORT = int(os.getenv("PORT", "8080"))

WEBSITE_ORIGIN = os.getenv(
    "WEBSITE_ORIGIN",
    "https://sarahshop.github.io"
).rstrip("/")

# PUBLIC receiving address only.
# NEVER put a seed phrase/private key here.
LTC_PAYMENT_ADDRESS = os.getenv("LTC_PAYMENT_ADDRESS", "").strip()

ORDER_EXPIRE_SECONDS = 2 * 60 * 60
MIN_CONFIRMATIONS = 1

web_runner = None


def cors_headers():
    # This API does not use cookies/auth credentials, so "*" is safe here
    # and avoids GitHub Pages / custom-domain origin mismatches.
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
        "Cache-Control": "no-store",
    }


def json_response(payload, status=200):
    return web.json_response(
        payload,
        status=status,
        headers=cors_headers()
    )


def ensure_order_store():
    data = load_data()

    if "ltc" not in data or not isinstance(data["ltc"], dict):
        data["ltc"] = {}

    if "orders" not in data or not isinstance(data["orders"], dict):
        data["orders"] = {}

    return data


def make_payment_order_id():
    stamp = time.strftime("%Y%m%d", time.gmtime())
    random_part = secrets.token_hex(5).upper()
    return f"SS-{stamp}-{random_part}"


async def get_ltc_usd_price():
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=litecoin&vs_currencies=usd"
    )

    timeout = aiohttp.ClientTimeout(total=12)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise RuntimeError("Could not retrieve Litecoin price.")

            payload = await response.json()
            price = float(payload["litecoin"]["usd"])

            if price <= 0:
                raise RuntimeError("Invalid Litecoin price.")

            return price


def active_reserved_satoshis(data):
    now = time.time()
    used = set()

    for order in data.get("orders", {}).values():
        if order.get("status") == "verified":
            continue

        created_at = float(order.get("created_at", 0))
        if now - created_at > ORDER_EXPIRE_SECONDS:
            continue

        sats = order.get("expected_satoshis")
        if isinstance(sats, int):
            used.add(sats)

    return used


def add_unique_satoshi_offset(base_satoshis, used_satoshis):
    # Give each active checkout a tiny unique amount so two payments sent
    # to the same public LTC address can still be matched to an order.
    for _ in range(500):
        offset = secrets.randbelow(900) + 100
        candidate = base_satoshis + offset

        if candidate not in used_satoshis:
            return candidate

    raise RuntimeError("Could not reserve a unique payment amount.")


async def find_matching_payment(expected_satoshis, created_at):
    if not LTC_PAYMENT_ADDRESS:
        raise RuntimeError("LTC_PAYMENT_ADDRESS is not configured.")

    url = (
        "https://api.blockcypher.com/v1/ltc/main/addrs/"
        f"{LTC_PAYMENT_ADDRESS}/full?limit=50"
    )

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise RuntimeError("Litecoin blockchain lookup failed.")

            wallet = await response.json()

    best = None

    for tx in wallet.get("txs", []):
        received = tx.get("received") or ""
        confirmations = int(tx.get("confirmations", 0) or 0)

        # BlockCypher returns ISO timestamps. Compare loosely by only
        # accepting transactions found after the order was created,
        # with a 5-minute allowance for clock/API timing differences.
        tx_epoch = 0

        try:
            from datetime import datetime
            tx_epoch = datetime.fromisoformat(
                received.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            tx_epoch = 0

        if tx_epoch and tx_epoch < (created_at - 300):
            continue

        paid_to_store = 0

        for output in tx.get("outputs", []):
            addresses = output.get("addresses") or []

            if LTC_PAYMENT_ADDRESS in addresses:
                paid_to_store += int(output.get("value", 0) or 0)

        if paid_to_store != expected_satoshis:
            continue

        match = {
            "txid": tx.get("hash"),
            "confirmations": confirmations,
            "received": received,
            "satoshis": paid_to_store,
        }

        if best is None or confirmations > best["confirmations"]:
            best = match

    return best


async def refresh_order_status(order_id):
    data = ensure_order_store()
    order = data["orders"].get(order_id)

    if not order:
        return None

    if order.get("status") == "verified":
        return order

    now = time.time()
    created_at = float(order.get("created_at", 0))

    if now - created_at > ORDER_EXPIRE_SECONDS:
        order["status"] = "expired"
        data["orders"][order_id] = order
        save_data(data)
        return order

    match = await find_matching_payment(
        int(order["expected_satoshis"]),
        created_at
    )

    if match:
        order["txid"] = match["txid"]
        order["confirmations"] = match["confirmations"]
        order["detected_at"] = time.time()

        if match["confirmations"] >= MIN_CONFIRMATIONS:
            order["status"] = "verified"
            order["verified_at"] = time.time()
        else:
            order["status"] = "detected"

        data["orders"][order_id] = order
        save_data(data)

    return order


async def api_health(request):
    return json_response(
        {
            "ok": True,
            "service": "Sarah Shop Payment API",
            "bot_ready": bot.is_ready(),
            "payment_address_configured": bool(LTC_PAYMENT_ADDRESS),
        }
    )


async def api_create_order(request):
    if not LTC_PAYMENT_ADDRESS:
        return json_response(
            {
                "ok": False,
                "error": "Store payment address is not configured yet."
            },
            503
        )

    try:
        body = await request.json()
    except Exception:
        return json_response(
            {"ok": False, "error": "Invalid JSON request."},
            400
        )

    product_id = str(body.get("product_id", "")).strip()
    product_name = str(body.get("product_name", "")).strip()
    option = str(body.get("option", "")).strip()

    try:
        usd_price = round(float(body.get("usd_price")), 2)
    except Exception:
        usd_price = 0

    if (
        not product_id
        or not product_name
        or not option
        or usd_price <= 0
        or usd_price > 10000
    ):
        return json_response(
            {"ok": False, "error": "Missing or invalid order details."},
            400
        )

    # IMPORTANT:
    # The checkout page will send these values from products.js.
    # In the next website step we will connect this endpoint directly
    # to the selected product.
    ltc_usd = await get_ltc_usd_price()

    base_satoshis = max(
        1,
        round((usd_price / ltc_usd) * 100_000_000)
    )

    data = ensure_order_store()
    used = active_reserved_satoshis(data)
    expected_satoshis = add_unique_satoshi_offset(
        base_satoshis,
        used
    )

    order_id = make_payment_order_id()
    created_at = time.time()

    order = {
        "order_id": order_id,
        "product_id": product_id,
        "product_name": product_name,
        "option": option,
        "usd_price": usd_price,
        "ltc_usd_rate": ltc_usd,
        "payment_address": LTC_PAYMENT_ADDRESS,
        "expected_satoshis": expected_satoshis,
        "expected_ltc": f"{expected_satoshis / 100_000_000:.8f}",
        "status": "waiting",
        "confirmations": 0,
        "txid": None,
        "created_at": created_at,
        "expires_at": created_at + ORDER_EXPIRE_SECONDS,
    }

    data["orders"][order_id] = order
    save_data(data)

    return json_response(
        {
            "ok": True,
            "order": {
                "order_id": order_id,
                "product_name": product_name,
                "option": option,
                "usd_price": usd_price,
                "ltc_usd_rate": ltc_usd,
                "payment_address": LTC_PAYMENT_ADDRESS,
                "expected_ltc": order["expected_ltc"],
                "status": "waiting",
                "expires_at": order["expires_at"],
            }
        },
        201
    )


async def api_get_order(request):
    order_id = request.match_info.get("order_id", "").strip()

    if not order_id:
        return json_response(
            {"ok": False, "error": "Order ID is required."},
            400
        )

    try:
        order = await refresh_order_status(order_id)
    except RuntimeError as exc:
        return json_response(
            {"ok": False, "error": str(exc)},
            502
        )

    if not order:
        return json_response(
            {"ok": False, "error": "Order not found."},
            404
        )

    return json_response(
        {
            "ok": True,
            "order": {
                "order_id": order["order_id"],
                "product_name": order["product_name"],
                "option": order["option"],
                "usd_price": order["usd_price"],
                "payment_address": order["payment_address"],
                "expected_ltc": order["expected_ltc"],
                "status": order["status"],
                "confirmations": order.get("confirmations", 0),
                "txid": order.get("txid"),
                "expires_at": order["expires_at"],
            }
        }
    )


async def api_options(request):
    return web.Response(
        status=204,
        headers=cors_headers()
    )


async def start_web_server():
    global web_runner

    if web_runner is not None:
        return

    app = web.Application()

    app.router.add_get("/", api_health)
    app.router.add_get("/health", api_health)

    app.router.add_post("/api/orders", api_create_order)
    app.router.add_get("/api/orders/{order_id}", api_get_order)

    app.router.add_route("OPTIONS", "/{tail:.*}", api_options)

    web_runner = web.AppRunner(app)
    await web_runner.setup()

    site = web.TCPSite(
        web_runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(f"Website API listening on port {PORT}")


# ============================================================
# TICKET MANAGER
# ============================================================
# This user can manage ALL tickets regardless of:
# - Seller role
# - Staff role
# - Ticket ownership
# - Claimed status
# - Deal completion
#
# DO NOT CHANGE THIS unless you want another person
# to have full ticket control.

TICKET_MANAGER_IDS = {
    1344469968338685983,
    749704343762239552,
}


# ============================================================
# NEW BLUE BANNER
# ============================================================

PANEL_BANNER = (
    "https://cdn.discordapp.com/attachments/"
    "1539768402648764426/"
    "1540767774312497323/"
    "ChatGPT_Image_Aug_22_2026_01_00_52_PM.png"
    "?ex=6a8b274f"
    "&is=6a89d5cf"
    "&hm=1252bf41da2c94f56d096a2717b47fecf45c92cc2d48126954a06bca668522ca&"
)


# ============================================================
# BLUE THEME
# ============================================================

BLUE = 0x3498DB
DARK_BLUE = 0x2980B9


# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.all()
intents.message_content = True

bot = commands.Bot(
    command_prefix="$",
    intents=intents,
    help_command=None
)
tickets = {}


# ============================================================
# PERMISSION HELPER
# ============================================================

def is_ticket_manager(user):
    return (
        user is not None
        and user.id in TICKET_MANAGER_IDS
    )


def is_staff(user):
    return any(
        role.id == STAFF_ROLE
        for role in getattr(user, "roles", [])
    )


def is_seller(user):
    return any(
        role.id == SELLER_ROLE
        for role in getattr(user, "roles", [])
    )


def can_manage_ticket(user):
    """
    Full ticket permissions.
    The configured Ticket Manager always has access.
    Staff can also manage tickets.
    """

    return (
        is_ticket_manager(user)
        or is_staff(user)
    )


# ============================================================
# DATA
# ============================================================

def load_data():

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except (
        FileNotFoundError,
        json.JSONDecodeError
    ):

        return {
            "ltc": {},
            "orders": {}
        }


def save_data(data):

    with open(
        DATA_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=4
        )


# ============================================================
# EMBED SYSTEM
# ============================================================

def make_embed(
    title=None,
    description=None,
    color=BLUE
):

    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )

    embed.set_footer(
        text=FOOTER
    )

    return embed


# ============================================================
# HELPERS
# ============================================================

def role(
    guild,
    role_id
):

    return guild.get_role(
        role_id
    )


def channel(
    guild,
    channel_id
):

    return guild.get_channel(
        channel_id
    )


# ============================================================
# CONFIRM DEAL
# ============================================================

class ConfirmDone(View):

    def __init__(
        self,
        seller_id
    ):

        super().__init__(
            timeout=None
        )

        self.seller_id = seller_id


    @discord.ui.button(
        label="Confirm",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_confirm"
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        ticket = tickets.get(
            interaction.channel.id
        )

        if not ticket:

            return await interaction.response.send_message(
                "Ticket data was lost after a restart.",
                ephemeral=True
            )

        # Ticket manager can confirm anything.
        # Otherwise only ticket owner can confirm.

        if (
            not is_ticket_manager(interaction.user)
            and interaction.user.id != ticket["owner"]
        ):

            return await interaction.response.send_message(
                "Only the ticket owner or a ticket manager can confirm.",
                ephemeral=True
            )

        seller = interaction.guild.get_member(
            self.seller_id
        )

        buyer = interaction.guild.get_member(
            ticket["owner"]
        )

        client_role = role(
            interaction.guild,
            CLIENT_ROLE
        )

        if client_role and buyer:

            await buyer.add_roles(
                client_role,
                reason="Completed a shop deal"
            )

        ticket["done"] = True

        if seller:

            await interaction.channel.edit(
                name=f"done-by-{seller.name}"[:100]
            )

        else:

            await interaction.channel.edit(
                name="deal-completed"[:100]
            )

        await interaction.response.send_message(
            embed=make_embed(
                "Deal Completed",
                "The deal has been successfully marked as completed."
            )
        )

        log = bot.get_channel(
            LOG_CHANNEL
        )

        if log and buyer:

            seller_text = (
                seller.mention
                if seller
                else "No seller assigned"
            )

            embed = make_embed(
                "Deal Completed Successfully",

                f"**Product**\n"
                f"> {ticket['product']}\n\n"

                f"**Buyer**\n"
                f"> {buyer.mention}\n\n"

                f"**Seller**\n"
                f"> {seller_text}\n\n"

                f"**Completed By**\n"
                f"> {interaction.user.mention}\n\n"

                f"**Time**\n"
                f"> <t:{int(interaction.created_at.timestamp())}:F>"
            )

            await log.send(
                embed=embed
            )

        await interaction.channel.send(
            embed=make_embed(
                "Vouch",
                "Please kindly vouch the seller.\n\n"
                "Use `$delete` to delete the ticket."
            )
        )


# ============================================================
# UNCLAIM REQUEST
# ============================================================

class UnclaimRequest(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )


    @discord.ui.button(
        label="Approve Unclaim",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_unclaim"
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        # Ticket manager or staff can approve.

        if not can_manage_ticket(
            interaction.user
        ):

            return await interaction.response.send_message(
                "Only staff or a ticket manager can approve this.",
                ephemeral=True
            )

        ticket = tickets.get(
            interaction.channel.id
        )

        if not ticket:

            return await interaction.response.send_message(
                "Ticket data was not found.",
                ephemeral=True
            )

        ticket["seller"] = None

        seller_role = role(
            interaction.guild,
            SELLER_ROLE
        )

        if seller_role:

            await interaction.channel.set_permissions(
                seller_role,
                view_channel=True,
                send_messages=True
            )

        req_cat = channel(
            interaction.guild,
            REQ_TICKET_CAT
        )

        await interaction.channel.edit(
            name="unclaimed-ticket",
            category=req_cat or interaction.channel.category
        )

        await interaction.response.send_message(
            embed=make_embed(
                "Ticket Unclaimed",
                "Ticket successfully unclaimed.\n\n"
                "Sellers can claim again."
            )
        )


# ============================================================
# MAIN PANEL
# ============================================================

class Panel(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )


    @discord.ui.button(
        label="Purchase",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_request"
    )
    async def request(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_modal(
            RequestForm()
        )


    @discord.ui.button(
        label="Support",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_support"
    )
    async def support(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_modal(
            SupportForm()
        )


# ============================================================
# PURCHASE FORM
# ============================================================

class RequestForm(
    Modal,
    title="Purchase Request"
):

    product = TextInput(
        label="Product / Service",
        placeholder="e.g. Streaming Services / Server Boost / Socials",
        min_length=2,
        max_length=50
    )

    budget = TextInput(
        label="Budget",
        placeholder="e.g. $10 / 0.5 BTC / 500 INR",
        max_length=50
    )

    info = TextInput(
        label="Additional Information",
        placeholder="Other information",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000
    )


    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        guild = interaction.guild

        category = channel(
            guild,
            REQ_TICKET_CAT
        )

        if not category:

            return await interaction.response.send_message(
                "Request ticket category is not configured.",
                ephemeral=True
            )

        ticket_name = "".join(
            c
            if c.isalnum() or c == "-"
            else "-"
            for c in self.product.value.lower().replace(
                " ",
                "-"
            )
        )[:90]

        overwrites = {

            guild.default_role:
                discord.PermissionOverwrite(
                    view_channel=False
                ),

            interaction.user:
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True
                )
        }

        seller = role(
            guild,
            SELLER_ROLE
        )

        if seller:

            overwrites[seller] = (
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True
                )
            )

        staff = role(
            guild,
            STAFF_ROLE
        )

        if staff:

            overwrites[staff] = (
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True
                )
            )

        # Give ALL ticket managers access automatically.

        for manager_id in TICKET_MANAGER_IDS:

            manager = guild.get_member(
                manager_id
            )

            if manager:

                overwrites[manager] = (
                    discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True
                    )
                )

        ch = await guild.create_text_channel(
            name=ticket_name or "request-ticket",
            category=category,
            overwrites=overwrites
        )

        tickets[ch.id] = {

            "owner":
                interaction.user.id,

            "seller":
                None,

            "product":
                self.product.value,

            "done":
                False
        }

        embed = make_embed(
            "Purchase Order",

            f"Hello {interaction.user.mention}!\n\n"

            f"Thanks for opening a Sarah Shop purchase ticket.\n\n"

            f"**Please send the following information:**\n"
            f"• **Order ID** from the Sarah Shop checkout page\n"
            f"• **Litecoin TXID** after sending payment\n"
            f"• **Product / Plan** you purchased\n"
            f"• Any **delivery or account details** needed for your order\n\n"

            f"**Ticket Details**\n\n"

            f"**Product**\n"
            f"{self.product.value}\n\n"

            f"**Budget**\n"
            f"{self.budget.value}\n\n"

            f"**Additional Information**\n"
            f"{self.info.value or 'None'}\n\n"

            f"⚠️ **Never send your seed phrase, recovery phrase, or private key.**\n\n"

            f"A seller will verify your payment and help complete your order shortly.\n\n"

            f"*Use the control buttons below to manage this ticket.*"
        )

        ping = seller.mention if seller else ""

        await ch.send(
            ping,
            embed=embed,
            view=RequestControls()
        )

        await interaction.response.send_message(
            f"Ticket created: {ch.mention}",
            ephemeral=True
        )


# ============================================================
# SUPPORT FORM
# ============================================================

class SupportForm(
    Modal,
    title="Support Ticket"
):

    reason = TextInput(
        label="Why are you making a ticket?",
        placeholder="Explain what you need help with.",
        style=discord.TextStyle.paragraph,
        min_length=2,
        max_length=4000
    )


    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        guild = interaction.guild

        category = channel(
            guild,
            SUPP_TICKET_CAT
        )

        if not category:

            return await interaction.response.send_message(
                "Support ticket category is not configured.",
                ephemeral=True
            )

        overwrites = {

            guild.default_role:
                discord.PermissionOverwrite(
                    view_channel=False
                ),

            interaction.user:
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True
                )
        }

        staff = role(
            guild,
            STAFF_ROLE
        )

        if staff:

            overwrites[staff] = (
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True
                )
            )

        # Give ALL ticket managers access automatically.

        for manager_id in TICKET_MANAGER_IDS:

            manager = guild.get_member(
                manager_id
            )

            if manager:

                overwrites[manager] = (
                    discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True
                    )
                )

        ch = await guild.create_text_channel(
            name=f"support-{interaction.user.name}".lower()[:90],
            category=category,
            overwrites=overwrites
        )

        tickets[ch.id] = {

            "owner":
                interaction.user.id,

            "seller":
                None,

            "product":
                "Support",

            "done":
                False
        }

        embed = make_embed(
            "Support Ticket",

            f"Hello {interaction.user.mention},\n\n"

            f"Your ticket has been created successfully "
            f"and our staff team has been notified.\n\n"

            f"**Reason**\n"
            f"{self.reason.value}\n\n"

            f"Please wait patiently — we aim to respond promptly."
        )

        await ch.send(
            f"{staff.mention} New support ticket created"
            if staff
            else "New support ticket created",

            embed=embed,

            # IMPORTANT:
            # Support tickets now also get the ticket controls.
            view=RequestControls()
        )

        await interaction.response.send_message(
            f"Support ticket created: {ch.mention}",
            ephemeral=True
        )


# ============================================================
# REQUEST CONTROLS
# ============================================================

class RequestControls(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )


    # ========================================================
    # CLAIM BUTTON
    # ========================================================

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_claim"
    )
    async def claim(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        ticket = tickets.get(
            interaction.channel.id
        )

        if not ticket:

            return await interaction.response.send_message(
                "Invalid ticket.",
                ephemeral=True
            )

        # Ticket manager can ALWAYS claim.

        if (
            not is_ticket_manager(interaction.user)
            and not is_seller(interaction.user)
        ):

            return await interaction.response.send_message(
                "Only sellers or a ticket manager can claim tickets.",
                ephemeral=True
            )

        if ticket["seller"]:

            # Ticket manager can take over an existing claim.

            if is_ticket_manager(
                interaction.user
            ):

                ticket["seller"] = interaction.user.id

            else:

                return await interaction.response.send_message(
                    "Ticket already claimed.",
                    ephemeral=True
                )

        else:

            ticket["seller"] = interaction.user.id

        claimed_cat = channel(
            interaction.guild,
            CLAIMED_CAT
        )

        await interaction.channel.edit(
            name=f"claimed-by-{interaction.user.name}"[:100],
            category=claimed_cat or interaction.channel.category
        )

        await interaction.response.send_message(
            embed=make_embed(
                "Ticket Claimed",
                f"{interaction.user.mention} has claimed this ticket."
            )
        )


    # ========================================================
    # CLOSE BUTTON
    # ========================================================

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_close"
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        ticket = tickets.get(
            interaction.channel.id
        )

        if not ticket:

            return await interaction.response.send_message(
                "This ticket is not loaded.",
                ephemeral=True
            )

        # Ticket manager can ALWAYS close.
        #
        # Everyone else:
        # - buyer can close
        # - staff can close

        if (
            not is_ticket_manager(interaction.user)
            and interaction.user.id != ticket["owner"]
            and not is_staff(interaction.user)
        ):

            return await interaction.response.send_message(
                "Only the buyer, staff, or a ticket manager can close this ticket.",
                ephemeral=True
            )

        owner = interaction.guild.get_member(
            ticket["owner"]
        )

        seller = (
            interaction.guild.get_member(
                ticket["seller"]
            )
            if ticket["seller"]
            else None
        )

        # Hide ticket from owner.

        if owner:

            await interaction.channel.set_permissions(
                owner,
                view_channel=False,
                send_messages=False
            )

        # Hide ticket from seller.

        if seller:

            await interaction.channel.set_permissions(
                seller,
                view_channel=False,
                send_messages=False
            )

        seller_role = role(
            interaction.guild,
            SELLER_ROLE
        )

        if seller_role:

            await interaction.channel.set_permissions(
                seller_role,
                view_channel=False
            )

        # Keep BOTH ticket managers able to view/manage closed tickets.
        for manager_id in TICKET_MANAGER_IDS:

            manager = interaction.guild.get_member(
                manager_id
            )

            if manager:

                await interaction.channel.set_permissions(
                    manager,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                )

        closed_cat = channel(
            interaction.guild,
            CLOSED_CAT
        )

        if closed_cat:

            await interaction.channel.edit(
                category=closed_cat
            )

        await interaction.response.send_message(
            embed=make_embed(
                "Ticket Closed",
                "🔒 This ticket has been closed."
            )
        )


# ============================================================
# PANEL COMMAND
# ============================================================

@bot.command()
async def panel(ctx):

    
    embed = make_embed(
        "PURCHASE CENTER",

        "**Ready to place an order?**\n\n"

        "Open a private ticket below and our team "
        "will help you out.\n\n"

        "❄️ **Purchase**\n"
        "Open a ticket to place an order or ask "
        "about a product.\n\n"

        "✦ **Support**\n"
        "Need help with an existing order, payment, "
        "verification, or anything else?\n\n"

        "**Before opening a ticket**\n"
        "• Please have your order/details ready\n"
        "• Be patient while waiting for staff\n"
        "• Please don't open multiple tickets for the same issue\n"
        "• Follow all server rules\n\n"

        "**Thank you for choosing Sarah Shop! 💙**"
    )

    embed.color = BLUE

    embed.set_image(
        url=PANEL_BANNER
    )

    await ctx.send(
        embed=embed,
        view=Panel()
    )


# ============================================================
# CLAIM COMMAND
# ============================================================

@bot.command()
async def claim(ctx):

    ticket = tickets.get(
        ctx.channel.id
    )

    if not ticket:

        return await ctx.send(
            embed=make_embed(
                "Not a Ticket",
                "This channel is not a ticket."
            )
        )

    # Manager OR seller.

    if (
        not is_ticket_manager(ctx.author)
        and not is_seller(ctx.author)
    ):

        return await ctx.send(
            embed=make_embed(
                "Seller Only",
                "Only sellers or a ticket manager can claim tickets."
            )
        )

    if ticket["seller"]:

        if not is_ticket_manager(ctx.author):

            return await ctx.send(
                embed=make_embed(
                    "Already Claimed",
                    "This ticket has already been claimed."
                )
            )

    ticket["seller"] = ctx.author.id

    claimed_cat = channel(
        ctx.guild,
        CLAIMED_CAT
    )

    await ctx.channel.edit(
        name=f"claimed-by-{ctx.author.name}"[:100],
        category=claimed_cat or ctx.channel.category
    )

    await ctx.send(
        embed=make_embed(
            "Ticket Claimed",
            f"Ticket claimed by {ctx.author.mention}."
        )
    )


# ============================================================
# UNCLAIM COMMAND
# ============================================================

@bot.command()
async def unclaim(ctx):

    ticket = tickets.get(
        ctx.channel.id
    )

    if not ticket:

        return await ctx.send(
            embed=make_embed(
                "Not a Ticket",
                "This is not a ticket."
            )
        )

    # Manager can directly unclaim.
    # Seller can request unclaim.

    if is_ticket_manager(ctx.author):

        ticket["seller"] = None

        seller_role = role(
            ctx.guild,
            SELLER_ROLE
        )

        if seller_role:

            await ctx.channel.set_permissions(
                seller_role,
                view_channel=True,
                send_messages=True
            )

        req_cat = channel(
            ctx.guild,
            REQ_TICKET_CAT
        )

        await ctx.channel.edit(
            name="unclaimed-ticket",
            category=req_cat or ctx.channel.category
        )

        return await ctx.send(
            embed=make_embed(
                "Ticket Unclaimed",
                "A ticket manager directly unclaimed this ticket."
            )
        )

    if ticket["seller"] != ctx.author.id:

        return await ctx.send(
            embed=make_embed(
                "Cannot Unclaim",
                "Only the seller who claimed this ticket "
                "or a ticket manager can unclaim it."
            )
        )

    embed = make_embed(
        "Unclaim Request",
        f"{ctx.author.mention} wants to unclaim this ticket."
    )

    await ctx.send(
        f"<@&{STAFF_ROLE}>",
        embed=embed,
        view=UnclaimRequest()
    )


# ============================================================
# DONE COMMAND
# ============================================================

@bot.command()
async def done(ctx):

    ticket = tickets.get(
        ctx.channel.id
    )

    if not ticket:

        return await ctx.send(
            embed=make_embed(
                "Not a Ticket",
                "This is not a ticket."
            )
        )

    # Ticket manager can complete any ticket.

    if (
        not is_ticket_manager(ctx.author)
        and ctx.author.id != ticket["owner"]
    ):

        return await ctx.send(
            embed=make_embed(
                "Not Allowed",
                "Only the ticket owner or a ticket manager can request deal completion."
            )
        )

    # Manager doesn't need a seller.

    if (
        not ticket["seller"]
        and not is_ticket_manager(ctx.author)
    ):

        return await ctx.send(
            embed=make_embed(
                "Not Claimed",
                "This ticket is not claimed yet."
            )
        )

    seller_id = (
        ticket["seller"]
        if ticket["seller"]
        else ctx.author.id
    )

    embed = make_embed(
        "Deal Complete Request",

        f"{ctx.author.mention}\n\n"
        "Only click **Confirm** if the deal has been completed."
    )

    await ctx.send(
        embed=embed,
        view=ConfirmDone(
            seller_id
        )
    )


# ============================================================
# DELETE COMMAND
# ============================================================

@bot.command(name="delete")
async def delete_ticket(ctx):

    ticket = tickets.get(
        ctx.channel.id
    )

    if not ticket:

        return await ctx.send(
            embed=make_embed(
                "Not a Ticket",
                "This is not a ticket."
            )
        )

    # ========================================================
    # IMPORTANT FIX
    #
    # Ticket manager can delete ANY ticket.
    #
    # No:
    # - seller requirement
    # - buyer requirement
    # - done requirement
    # ========================================================

    if (
        not is_ticket_manager(ctx.author)
        and not is_staff(ctx.author)
    ):

        if ctx.author.id != ticket["owner"]:

            return await ctx.send(
                embed=make_embed(
                    "Not Allowed",
                    "Only the ticket owner, staff, or a ticket manager can delete this ticket."
                )
            )

        if not ticket.get("done"):

            return await ctx.send(
                embed=make_embed(
                    "Deal Not Completed",
                    "The deal must be completed before the ticket can be deleted."
                )
            )

    # ========================================================
    # TRANSCRIPT
    # ========================================================

    try:

        transcript = await chat_exporter.export(
            ctx.channel
        )

    except Exception:

        transcript = None

    log = bot.get_channel(
        TRANSCRIPT_CHANNEL
    )

    if transcript and log:

        transcript_file = discord.File(
            io.BytesIO(
                transcript.encode()
            ),
            filename="transcript.html"
        )

        seller_text = (
            f"<@{ticket['seller']}>"
            if ticket.get("seller")
            else "No seller"
        )

        embed = make_embed(
            "Ticket Transcript",

            f"**Owner:** <@{ticket['owner']}>\n"
            f"**Seller:** {seller_text}\n"
            f"**Deal:** {ticket['product']}\n"
            f"**Deleted By:** {ctx.author.mention}"
        )

        await log.send(
            embed=embed,
            file=transcript_file
        )

    # Remove from memory.

    tickets.pop(
        ctx.channel.id,
        None
    )

    # Delete channel.

    await ctx.channel.delete(
        reason=f"Ticket deleted by {ctx.author} ({ctx.author.id})"
    )


# ============================================================
# CALCULATOR
# ============================================================

@bot.command()
async def calc(
    ctx,
    *,
    equation: str
):

    try:

        result = eval(
            equation,
            {"__builtins__": {}},
            math.__dict__
        )

        await ctx.send(
            embed=make_embed(
                "🧮 Calculator",

                f"**Equation:**\n"
                f"`{equation}`\n\n"

                f"**Result:**\n"
                f"`{result}`"
            )
        )

    except Exception:

        await ctx.send(
            embed=make_embed(
                "Calculator Error",
                "❌ Invalid equation."
            )
        )


# ============================================================
# LTC PRICE
# ============================================================

@bot.command()
async def ltcprice(ctx):

    async with aiohttp.ClientSession() as session:

        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=litecoin&vs_currencies=usd"
        ) as r:

            if r.status != 200:

                return await ctx.send(
                    embed=make_embed(
                        "Error",
                        "Could not get LTC price."
                    )
                )

            price = (
                await r.json()
            )["litecoin"]["usd"]

    await ctx.send(
        embed=make_embed(
            "📈 Litecoin Price",
            f"**${price} USD**"
        )
    )


# ============================================================
# SET LTC
# ============================================================

@bot.command()
async def setltc(
    ctx,
    address: str
):

    data = load_data()

    uid = str(
        ctx.author.id
    )

    if uid in data["ltc"]:

        return await ctx.send(
            embed=make_embed(
                "Already Saved",
                "❌ LTC address already saved."
            )
        )

    data["ltc"][uid] = address

    save_data(data)

    await ctx.send(
        embed=make_embed(
            "Saved Successfully",
            "✅ LTC address saved successfully."
        )
    )


# ============================================================
# LTC ADDRESS
# ============================================================

@bot.command()
async def ltc(ctx):

    data = load_data()

    uid = str(
        ctx.author.id
    )

    if uid not in data["ltc"]:

        return await ctx.send(
            embed=make_embed(
                "No LTC Saved",
                "❌ No LTC address saved."
            )
        )

    await ctx.send(
        embed=make_embed(
            "Your LTC Address",
            data["ltc"][uid]
        )
    )


# ============================================================
# LTC BALANCE
# ============================================================

@bot.command(name="bal")
async def bal(
    ctx,
    address: str
):

    async with aiohttp.ClientSession() as session:

        url = (
            "https://api.blockcypher.com/"
            f"v1/ltc/main/addrs/{address}/balance"
        )

        async with session.get(url) as r:

            if r.status != 200:

                return await ctx.send(
                    embed=make_embed(
                        "Wallet Error",
                        "❌ Invalid LTC address or API error."
                    )
                )

            wallet = await r.json()

        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=litecoin&vs_currencies=usd"
        ) as p:

            if p.status != 200:

                return await ctx.send(
                    embed=make_embed(
                        "Price Error",
                        "❌ Could not retrieve LTC price."
                    )
                )

            price = (
                await p.json()
            )["litecoin"]["usd"]

    confirmed_ltc = (
        wallet["balance"]
        / 100_000_000
    )

    unconfirmed_ltc = (
        wallet["unconfirmed_balance"]
        / 100_000_000
    )

    embed = make_embed(
        "📦 Wallet Balance"
    )

    embed.add_field(
        name="Confirmed Balance",
        value=(
            f"**${confirmed_ltc * price:.2f}**\n"
            f"**{confirmed_ltc} LTC**"
        ),
        inline=False
    )

    embed.add_field(
        name="Unconfirmed Balance",
        value=(
            f"**${unconfirmed_ltc * price:.2f}**\n"
            f"**{unconfirmed_ltc} LTC**"
        ),
        inline=False
    )

    await ctx.send(
        embed=embed
    )


# ============================================================
# LTC TRANSACTION
# ============================================================

@bot.command(name="tx")
async def txid(
    ctx,
    txid: str
):

    async with aiohttp.ClientSession() as session:

        url = (
            "https://api.blockcypher.com/"
            f"v1/ltc/main/txs/{txid}"
        )

        async with session.get(url) as r:

            if r.status != 200:

                return await ctx.send(
                    embed=make_embed(
                        "Transaction Error",
                        "❌ Invalid TXID or API error."
                    )
                )

            data = await r.json()

    confirmations = data.get(
        "confirmations",
        0
    )

    total_ltc = (
        data.get("total", 0)
        / 100_000_000
    )

    fees_ltc = (
        data.get("fees", 0)
        / 100_000_000
    )

    received_time = data.get(
        "received",
        "Unknown"
    )

    embed = make_embed(
        "🔗 Litecoin Transaction Info"
    )

    embed.add_field(
        name="TXID",
        value=f"`{txid}`",
        inline=False
    )

    embed.add_field(
        name="Confirmations",
        value=str(confirmations),
        inline=True
    )

    embed.add_field(
        name="Amount (LTC)",
        value=str(total_ltc),
        inline=True
    )

    embed.add_field(
        name="Fees (LTC)",
        value=str(fees_ltc),
        inline=True
    )

    embed.add_field(
        name="Received",
        value=received_time,
        inline=False
    )

    embed.add_field(
        name="Status",
        value=(
            "✅ Confirmed"
            if confirmations >= 1
            else "⏳ Unconfirmed"
        ),
        inline=True
    )

    await ctx.send(
        embed=embed
    )


# ============================================================
# UPI QR
# ============================================================

@bot.command()
async def mqr(
    ctx,
    upi: str,
    amount: float
):

    qr_data = (
        f"upi://pay?"
        f"pa={upi}"
        f"&am={amount}"
        f"&cu=INR"
    )

    qr = qrcode.make(
        qr_data
    )

    buf = BytesIO()

    qr.save(
        buf,
        format="PNG"
    )

    buf.seek(0)

    file = discord.File(
        buf,
        filename="upi.png"
    )

    embed = make_embed(
        "💳 UPI QR Code",

        f"**UPI ID:** `{upi}`\n"
        f"**Amount:** ₹{amount}"
    )

    # Your blue banner.

    embed.set_image(
        url=PANEL_BANNER
    )

    await ctx.send(
        embed=embed,
        file=file
    )


# ============================================================
# HELP
# ============================================================

@bot.command()
async def help(ctx):

    embed = make_embed(
        "📘 Bot Commands",

        "**General Commands**\n\n"

        "**$calc <equation>**\n"
        "Calculator\n\n"

        "**$setltc <address>**\n"
        "Save LTC address\n\n"

        "**$ltc**\n"
        "Show saved LTC address\n\n"

        "**$bal <ltc-address>**\n"
        "Check wallet balance\n\n"

        "**$ltcprice**\n"
        "Check LTC price\n\n"

        "**$tx <txid>**\n"
        "Transaction information\n\n"

        "**$mqr <upi-id> <amount>**\n"
        "Generate UPI QR\n\n"

        "**Owner / Seller Commands**\n\n"

        "**$panel**\n"
        "Send the purchase panel\n\n"

        "**$claim**\n"
        "Claim a ticket\n\n"

        "**$unclaim**\n"
        "Request an unclaim\n\n"

        "**$done**\n"
        "Request deal completion\n\n"

        "**$delete**\n"
        "Save transcript and delete ticket"
    )

    await ctx.send(
        embed=embed
    )


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    await start_web_server()

    bot.add_view(
        Panel()
    )

    bot.add_view(
        RequestControls()
    )

    bot.add_view(
        UnclaimRequest()
    )

    print(
        f"Logged in as {bot.user} ({bot.user.id})"
    )

    print(
        "Bot is ready."
    )

    print(
        f"Ticket Manager IDs: {sorted(TICKET_MANAGER_IDS)}"
    )


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":

    if not TOKEN:

        raise RuntimeError(
            "DISCORD_TOKEN is missing. "
            "Put your new bot token in .env."
        )

    bot.run(TOKEN)