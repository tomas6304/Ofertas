#!/usr/bin/env python3
"""Busca ofertas de perfumería y belleza en tiendas argentinas y avisa por mail / Telegram / ntfy.

Uso:
  python3 scraper/scrape.py              # corrida normal (guarda data/ y avisa)
  python3 scraper/scrape.py --diagnose   # solo prueba cada tienda, no guarda nada
"""
import datetime as dt
import html as ihtml
import json
import os
import re
import smtplib
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PAUSA = 0.15          # pausa corta entre pedidos de un mismo hilo
SEG_POR_TIENDA = 240  # tope de tiempo por tienda (para que la corrida sea corta)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_loc = threading.local()
ML = re.compile(r"(\d+(?:[.,]\d+)?)\s*ml", re.I)
LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


# ---------- red ----------
def sesion():
    if not hasattr(_loc, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _loc.s = s
    return _loc.s


def get(url, intentos=2):
    for i in range(intentos):
        try:
            r = sesion().get(url, timeout=20)
        except requests.RequestException:
            time.sleep(1)
            continue
        if r.status_code in (429, 503):
            time.sleep(3 * (i + 1))
            continue
        return r
    return None


def vencido(t):
    return time.time() > t["_fin"]


# ---------- utilidades ----------
def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def parse_ml(txt):
    m = ML.search(txt or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return int(v) if v == int(v) else v


def num_ar(txt):
    t = re.sub(r"[^\d,.]", "", str(txt or ""))
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def a_num(v):
    if v is None or v == "":
        return None
    if isinstance(v, str) and "," in v:
        return num_ar(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return num_ar(v)


# ---------- categorías ----------
CATS = [
    ("pelo", re.compile(r"shampoo|champu|acondicionador|capilar|cabello|\bpelo\b|enjuague|ampolla|tintura|coloracion|alisado|keratina|peinar|secador|planchita|cepillo")),
    ("piel", re.compile(r"facial|rostro|\bserum\b|\bsuero\b|hidratante|protector solar|\bfps\b|anti ?age|antiedad|contorno de ojos|tonico|limpiador|desmaquillante|micelar|skincare|cuidado de la piel|acne|arrugas")),
    ("maquillaje", re.compile(r"maquill|labial|corrector|rimel|pestan|sombra|rubor|iluminador|delineador|polvo compacto|polvo suelto|gloss|paleta|esmalte|cejas|\bprimer\b|bronzer|bb cream|cc cream|tinta|lapiz|\bunas\b")),
    ("cuerpo", re.compile(r"corporal|body lotion|body cream|desodorante|antitranspirante|jabon|ducha|\bbano\b|manos|\bpies\b|depilac|bronceador|autobronceante|talco|repelente|afeit|barba|after shave")),
    ("perfumes", re.compile(r"perfum|fragancia|eau de|\bedp\b|\bedt\b|\bedc\b|colonia|body mist|body splash|bruma|extrait|parfum|decant")),
]
HOMBRE = re.compile(r"\b(hombres?|men|man|masculin[oa]s?|homme|caballeros?|varon(es)?)\b|for him")
MUJER = re.compile(r"\b(mujer(es)?|women|woman|femenin[oa]s?|femme|damas?)\b|for her")


def clasificar(extra, nombre, t):
    for texto in (norm(extra), norm(nombre)):
        if not texto.strip():
            continue
        for cat, rx in CATS:
            if rx.search(texto):
                return cat
    return "perfumes" if t.get("solo_perfumes") else "otros"


def genero_de(extra, nombre, t):
    for texto in (norm(extra), norm(nombre)):
        if re.search(r"unisex|sin genero", texto):
            return "unisex"
        h, m = bool(HOMBRE.search(texto)), bool(MUJER.search(texto))
        if h and not m:
            return "hombre"
        if m and not h:
            return "mujer"
        if h and m:
            return "unisex"
    return "unisex" if t.get("solo_perfumes") else ""


def rec(t, sku, marca, nombre, precio, lista, stock, url, img, base, extra=""):
    precio = a_num(precio)
    if not precio or precio <= 0:
        return None
    lista = a_num(lista)
    if lista is not None and lista <= precio:
        lista = None
    nombre = re.sub(r"\s+", " ", ihtml.unescape(nombre or "")).strip()
    if url and url.startswith("/"):
        url = base + url
    if img and img.startswith("//"):
        img = "https:" + img
    cat = clasificar(extra, nombre, t)
    r = {
        "id": f"{t['id']}:{sku}", "tienda": t["id"], "marca": ihtml.unescape(marca or "").strip(),
        "nombre": nombre, "ml": parse_ml(nombre), "precio": round(precio),
        "lista": round(lista) if lista else None,
        "descuento": round((1 - precio / lista) * 100) if lista else 0,
        "stock": bool(stock), "url": url, "img": img, "cat": cat,
        "genero": genero_de(extra, nombre, t) if cat == "perfumes" else "",
    }
    if t.get("nicho"):
        r["nicho"] = True
    return r


# ---------- adaptadores ----------
def vtex(t, base):
    out, paso, hilos = [], 50, 4

    def pagina(desde):
        r = get(f"{base}/api/catalog_system/pub/products/search?_from={desde}&_to={desde + paso - 1}")
        if r is None or r.status_code not in (200, 206):
            return None
        try:
            return r.json()
        except ValueError:
            return None

    with ThreadPoolExecutor(hilos) as pool:
        for lote in range(0, 2500, paso * hilos):
            if vencido(t):
                break
            paginas = list(pool.map(pagina, [lote + i * paso for i in range(hilos)]))
            fin = False
            for prods in paginas:
                if not prods:
                    fin = True
                    break
                for p in prods:
                    extra = " ".join(p.get("categories") or [])
                    for it in p.get("items", []):
                        sellers = it.get("sellers") or []
                        if not sellers:
                            continue
                        o = sellers[0].get("commertialOffer", {})
                        imgs = it.get("images") or []
                        x = rec(t, it.get("itemId"), p.get("brand"),
                                it.get("nameComplete") or p.get("productName"),
                                o.get("Price"), o.get("ListPrice"),
                                (o.get("AvailableQuantity") or 0) > 0,
                                p.get("link"), imgs[0].get("imageUrl") if imgs else None, base, extra)
                        if x:
                            out.append(x)
            if fin:
                break
    return out


def shopify(t, base):
    out = []
    for pagina in range(1, 41):
        if vencido(t):
            break
        r = get(f"{base}/products.json?limit=250&page={pagina}")
        if r is None or r.status_code != 200:
            break
        prods = r.json().get("products", [])
        if not prods:
            break
        for p in prods:
            img = (p.get("images") or [{}])[0].get("src")
            tags = p.get("tags") or ""
            extra = f'{p.get("product_type", "")} {" ".join(tags) if isinstance(tags, list) else tags}'
            for v in p.get("variants", []):
                nombre = p["title"] if v.get("title") == "Default Title" else f'{p["title"]} {v.get("title")}'
                x = rec(t, v["id"], p.get("vendor"), nombre, v.get("price"),
                        v.get("compare_at_price"), v.get("available"),
                        f'{base}/products/{p["handle"]}', img, base, extra)
                if x:
                    out.append(x)
        time.sleep(PAUSA)
    return out


def woo(t, base):
    """WooCommerce (WordPress): API pública de la tienda."""
    out = []
    for pagina in range(1, 61):
        if vencido(t):
            break
        r = get(f"{base}/wp-json/wc/store/v1/products?per_page=100&page={pagina}")
        if r is None or r.status_code != 200:
            break
        try:
            prods = r.json()
        except ValueError:
            break
        if not prods:
            break
        for p in prods:
            pr = p.get("prices") or {}
            div = 10 ** int(pr.get("currency_minor_unit", 2))

            def mon(x):
                try:
                    return float(x) / div
                except (TypeError, ValueError):
                    return None

            imgs = p.get("images") or []
            marcas = " ".join(b.get("name", "") for b in p.get("brands", []) or [])
            extra = " ".join(c.get("name", "") for c in p.get("categories", []) or [])
            x = rec(t, p.get("id"), marcas, p.get("name"), mon(pr.get("price")),
                    mon(pr.get("regular_price")), p.get("is_in_stock", True),
                    p.get("permalink"), imgs[0].get("src") if imgs else None, base, extra)
            if x:
                out.append(x)
        time.sleep(PAUSA)
    return out


def sitemap_urls(base, t):
    r = get(base + "/sitemap.xml")
    if r is None or r.status_code != 200:
        return []

    def locs(txt):
        try:
            root = ET.fromstring(txt.encode("utf-8"))
        except ET.ParseError:
            return []
        return [e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text]

    primeros = locs(r.text)
    subs = [u for u in primeros if u.endswith(".xml") or "sitemap" in u.split("/")[-1]]
    urls = []
    if subs:
        prod = [u for u in subs if "product" in u.lower()] or subs
        for s in prod[:8]:
            if vencido(t):
                break
            rr = get(s)
            if rr is not None and rr.status_code == 200:
                urls += locs(rr.text)
    else:
        urls = primeros
    return [u for u in urls if not u.endswith(".xml")][:20000]


def productos_ld(html_txt):
    out = []

    def walk(x):
        if isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            tp = x.get("@type")
            if tp == "Product" or (isinstance(tp, list) and "Product" in tp):
                out.append(x)
            for v in x.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for m in LD.finditer(html_txt):
        try:
            walk(json.loads(m.group(1)))
        except ValueError:
            pass
    return out


def leer_producto(t, base, u):
    if vencido(t):
        return None
    r = get(u, 1)
    time.sleep(PAUSA)
    if r is None or r.status_code != 200:
        return None
    prods = productos_ld(r.text)
    if not prods:
        return None
    p = prods[0]
    offers = p.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    precio = offers.get("price") or offers.get("lowPrice")
    if not precio:
        m = re.search(r'property="product:price:amount"\s+content="([^"]+)"', r.text)
        precio = m.group(1) if m else None
    lista = None
    specs = offers.get("priceSpecification") or []
    if isinstance(specs, dict):
        specs = [specs]
    for sp in specs:
        if "list" in str(sp.get("priceType", "")).lower():
            lista = sp.get("price")
    if not lista:
        m = re.search(r"js-compare-price-display[^>]*>\s*([^<]+)<", r.text)
        lista = num_ar(m.group(1)) if m else None
    marca = p.get("brand")
    marca = marca.get("name") if isinstance(marca, dict) else marca
    img = p.get("image")
    img = img[0] if isinstance(img, list) and img else img
    cat = p.get("category")
    extra = " ".join(cat) if isinstance(cat, list) else (cat or "")
    sku = u.rstrip("/").split("/")[-1]
    return rec(t, sku, marca, p.get("name"), precio, lista,
               "InStock" in str(offers.get("availability", "")), u,
               img if isinstance(img, str) else None, base, extra)


def generico(t, base):
    """Tiendas sin API pública: lee el sitemap y los datos de cada producto.
    Lee un lote por corrida y va rotando, así cubre todo el catálogo en varias corridas."""
    todas = sitemap_urls(base, t)
    if not todas:
        return []
    lote = t.get("max_paginas", 250)
    ini = t.get("_offset", 0) % len(todas)
    elegidas = (todas + todas)[ini:ini + lote] if len(todas) > lote else todas
    t["_offset_nuevo"] = (ini + lote) % len(todas)
    with ThreadPoolExecutor(8) as pool:
        leidos = list(pool.map(lambda u: leer_producto(t, base, u), elegidas))
    nuevos, vistos = [], set()
    for x in leidos:
        if x and x["id"] not in vistos:
            vistos.add(x["id"])
            nuevos.append(x)
    conjunto = set(todas)
    viejos = [p for p in t.get("_prev", []) if p["url"] in conjunto and p["id"] not in vistos]
    return nuevos + viejos


def sondear(base):
    """Para entender por qué una tienda no devolvió nada."""
    partes = []
    home = ""
    for nombre, ruta in [("home", "/"), ("vtex", "/api/catalog_system/pub/products/search?_from=0&_to=0"),
                         ("shopify", "/products.json?limit=1"), ("woo", "/wp-json/wc/store/v1/products?per_page=1"),
                         ("sitemap", "/sitemap.xml")]:
        try:
            r = sesion().get(base + ruta, timeout=15)
            partes.append(f"{nombre}={r.status_code}")
            if nombre == "home" and r.status_code == 200:
                home = r.text.lower()
        except requests.RequestException as e:
            partes.append(f"{nombre}={type(e).__name__}")
    for k, pat in [("vtex", "vtex"), ("shopify", "cdn.shopify.com"), ("woocommerce", "wp-content"),
                   ("tiendanube", "tiendanube|nuvemshop"), ("magento", "magento|mage-")]:
        if home and re.search(pat, home):
            partes.append(f"sitio={k}")
            break
    return " ".join(partes)


def detectar(base):
    r = get(f"{base}/api/catalog_system/pub/products/search?_from=0&_to=0")
    if r is not None and r.status_code in (200, 206) and r.text.strip().startswith("["):
        return "vtex"
    r = get(f"{base}/products.json?limit=1")
    if r is not None and r.status_code == 200 and '"products"' in r.text[:300]:
        return "shopify"
    r = get(f"{base}/wp-json/wc/store/v1/products?per_page=1")
    if r is not None and r.status_code == 200 and r.text.strip().startswith("["):
        return "woo"
    return "generico"


ADAPTADORES = {"vtex": vtex, "shopify": shopify, "woo": woo, "generico": generico}


def trabajar(t):
    """Procesa una tienda (corre en su propio hilo)."""
    t = dict(t)
    t["_fin"] = time.time() + t.get("max_segundos", SEG_POR_TIENDA)
    base = t["url"].rstrip("/")
    inicio = time.time()
    try:
        plat = t.get("plataforma", "auto")
        if plat == "auto":
            plat = detectar(base)
        items = ADAPTADORES[plat](t, base)
        err = None if items else "0 productos · " + sondear(base)
    except Exception as e:  # una tienda rota no frena a las demás
        plat, items, err = "?", [], f"{type(e).__name__}: {e}"
    return t, plat, items, err, time.time() - inicio


# ---------- avisos ----------
def plata(n):
    return "$" + f"{n:,.0f}".replace(",", ".")


def telegram(texto):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20,
                      data={"chat_id": chat, "text": texto, "disable_web_page_preview": "true"})
    if not r.ok:
        print("Telegram falló:", r.status_code, r.text[:200])


def ntfy(texto):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return
    h = {"Title": "Ofertas de perfumes"}
    if os.getenv("SITE_URL"):
        h["Click"] = os.getenv("SITE_URL")
    r = requests.post(f"https://ntfy.sh/{topic}", data=texto.encode("utf-8"), headers=h, timeout=20)
    if not r.ok:
        print("ntfy falló:", r.status_code)


def correo(texto):
    user, clave, dest = os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"), os.getenv("EMAIL_TO")
    if not (user and clave):
        return
    m = EmailMessage()
    m["Subject"] = "Ofertas de perfumes"
    m["From"] = user
    m["To"] = dest or user
    m.set_content(texto)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, clave)
            s.send_message(m)
    except Exception as e:
        print("Correo falló:", e)


def avisar(nuevas, nombres):
    nuevas = sorted(nuevas, key=lambda p: -p["descuento"])
    for i in range(0, len(nuevas), 10):
        lote = nuevas[i:i + 10]
        lineas = [f"🔥 {len(nuevas)} ofertas nuevas" if i == 0 else "…continúa"]
        for p in lote:
            ml = f" {p['ml']}ml" if p["ml"] and f"{p['ml']}" not in p["nombre"] else ""
            lineas.append(f"\n-{p['descuento']}% {p['nombre'][:70]}{ml}\n"
                          f"{nombres[p['tienda']]}: {plata(p['precio'])} (lista {plata(p['lista'])})\n{p['url']}")
        if os.getenv("SITE_URL") and i == 0:
            lineas.append(f"\nVer todas: {os.getenv('SITE_URL')}")
        texto = "\n".join(lineas)
        telegram(texto)
        ntfy(texto)
        correo(texto)


# ---------- principal ----------
def cargar(path, defecto):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defecto


def main():
    diagnostico = "--diagnose" in sys.argv
    t0 = time.time()
    previo = cargar(DATA / "precios.json", {"productos": [], "actualizado": None})
    previos = {p["id"]: p for p in previo.get("productos", [])}
    rot = previo.get("rot", {})
    hist = cargar(DATA / "historial.json", {})
    nombres = {t["id"]: t["nombre"] for t in CFG["tiendas"]}

    tareas = []
    for t in CFG["tiendas"]:
        t = dict(t)
        t["_offset"] = rot.get(t["id"], 0)
        t["_prev"] = [p for p in previos.values() if p["tienda"] == t["id"]]
        tareas.append(t)

    with ThreadPoolExecutor(6) as pool:
        resultados = list(pool.map(trabajar, tareas))

    productos, estado, rot_nuevo = [], {}, {}
    for t, plat, items, err, seg in resultados:
        con_desc = sum(1 for p in items if p["descuento"] > 0)
        print(f"== {t['nombre']} ({t['url']})\n   plataforma: {plat} | productos: {len(items)} | "
              f"con descuento: {con_desc} | {seg:.0f}s" + (f" | ERROR: {err}" if err else ""))
        if diagnostico and items:
            for p in sorted(items, key=lambda p: -p["descuento"])[:3]:
                print(f"   ej: {p['nombre'][:50]} {plata(p['precio'])} lista={p['lista']} -{p['descuento']}% "
                      f"[{p['cat']}/{p['genero']}]")
        if "_offset_nuevo" in t:
            rot_nuevo[t["id"]] = t["_offset_nuevo"]
        if not items:  # conservar lo último que se sabía de esa tienda
            items = [p for p in previos.values() if p["tienda"] == t["id"]]
        estado[t["id"]] = {"nombre": t["nombre"], "ok": err is None, "plataforma": plat,
                           "cantidad": len(items), "error": err}
        productos += items
    print(f"Tiempo total: {time.time() - t0:.0f}s")

    if diagnostico:
        return

    ahora = dt.datetime.now(dt.timezone.utc)
    hoy = ahora.strftime("%Y-%m-%d")
    for p in productos:
        h = hist.setdefault(p["id"], [])
        if not h or h[-1][1] != p["precio"]:
            h.append([hoy, p["precio"]])
        del h[:-60]
    ids = {p["id"] for p in productos}
    hist = {k: v for k, v in hist.items() if k in ids}

    # ofertas nuevas: descuento alto y (producto nuevo, antes no era oferta, o bajó 10%+).
    # Una tienda que se agrega por primera vez no dispara avisos de todo su catálogo.
    umbral = CFG.get("umbral_aviso", 40)
    tiendas_previas = {p["tienda"] for p in previos.values()}
    nuevas = []
    if previo.get("actualizado"):
        for p in productos:
            if p["tienda"] not in tiendas_previas or not p["stock"] or p["descuento"] < umbral:
                continue
            a = previos.get(p["id"])
            if a is None or a["descuento"] < umbral or p["precio"] <= a["precio"] * 0.9:
                nuevas.append(p)
    if nuevas:
        avisar(nuevas, nombres)
    print(f"Ofertas nuevas avisadas: {len(nuevas)}")

    salida = {"actualizado": ahora.isoformat(), "tiendas": estado, "rot": rot_nuevo, "productos": productos}
    (DATA / "precios.json").write_text(json.dumps(salida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA / "historial.json").write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
