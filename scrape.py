#!/usr/bin/env python3
"""Busca ofertas de perfumes en tiendas argentinas y avisa por Telegram / ntfy.

Uso:
  python3 scraper/scrape.py              # corrida normal (guarda data/ y avisa)
  python3 scraper/scrape.py --diagnose   # solo prueba cada tienda, no guarda nada
"""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PAUSA = 0.8  # segundos entre pedidos: no saturar las tiendas

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; OfertasPerfumes/1.0; uso personal)",
    "Accept-Language": "es-AR,es;q=0.9",
})
ML = re.compile(r"(\d+(?:[.,]\d+)?)\s*ml", re.I)
LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


# ---------- utilidades ----------
def get(url):
    for i in range(3):
        try:
            r = S.get(url, timeout=25)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code in (403, 429, 503):
            time.sleep(4 * (i + 1))
            continue
        return r
    return None


def parse_ml(txt):
    m = ML.search(txt or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return int(v) if v == int(v) else v


def num_ar(txt):
    t = re.sub(r"[^\d,.]", "", txt or "")
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


def rec(tienda, sku, marca, nombre, precio, lista, stock, url, img, base):
    try:
        precio = float(precio)
    except (TypeError, ValueError):
        return None
    if precio <= 0:
        return None
    try:
        lista = float(lista) if lista else None
    except (TypeError, ValueError):
        lista = None
    if lista is not None and lista <= precio:
        lista = None
    nombre = re.sub(r"\s+", " ", nombre or "").strip()
    if url and url.startswith("/"):
        url = base + url
    if img and img.startswith("//"):
        img = "https:" + img
    return {
        "id": f"{tienda}:{sku}", "tienda": tienda, "marca": (marca or "").strip(),
        "nombre": nombre, "ml": parse_ml(nombre), "precio": round(precio),
        "lista": round(lista) if lista else None,
        "descuento": round((1 - precio / lista) * 100) if lista else 0,
        "stock": bool(stock), "url": url, "img": img,
    }


# ---------- adaptadores ----------
def vtex(t, base):
    out, paso = [], 50
    for desde in range(0, 2500, paso):
        r = get(f"{base}/api/catalog_system/pub/products/search?_from={desde}&_to={desde + paso - 1}")
        if r is None or r.status_code not in (200, 206):
            break
        try:
            prods = r.json()
        except ValueError:
            break
        if not prods:
            break
        for p in prods:
            for it in p.get("items", []):
                sellers = it.get("sellers") or []
                if not sellers:
                    continue
                o = sellers[0].get("commertialOffer", {})
                imgs = it.get("images") or []
                x = rec(t["id"], it.get("itemId"), p.get("brand"),
                        it.get("nameComplete") or p.get("productName"),
                        o.get("Price"), o.get("ListPrice"),
                        (o.get("AvailableQuantity") or 0) > 0,
                        p.get("link"), imgs[0].get("imageUrl") if imgs else None, base)
                if x:
                    out.append(x)
        time.sleep(PAUSA)
    return out


def shopify(t, base):
    out = []
    for pagina in range(1, 41):
        r = get(f"{base}/products.json?limit=250&page={pagina}")
        if r is None or r.status_code != 200:
            break
        prods = r.json().get("products", [])
        if not prods:
            break
        for p in prods:
            img = (p.get("images") or [{}])[0].get("src")
            for v in p.get("variants", []):
                nombre = p["title"] if v.get("title") == "Default Title" else f'{p["title"]} {v.get("title")}'
                x = rec(t["id"], v["id"], p.get("vendor"), nombre, v.get("price"),
                        v.get("compare_at_price"), v.get("available"),
                        f'{base}/products/{p["handle"]}', img, base)
                if x:
                    out.append(x)
        time.sleep(PAUSA)
    return out


def sitemap_urls(base, limite):
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
        for s in prod[:6]:
            rr = get(s)
            if rr is not None and rr.status_code == 200:
                urls += locs(rr.text)
            time.sleep(PAUSA)
    else:
        urls = primeros
    return [u for u in urls if not u.endswith(".xml")][:limite]


def productos_ld(html):
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

    for m in LD.finditer(html):
        try:
            walk(json.loads(m.group(1)))
        except ValueError:
            pass
    return out


def generico(t, base):
    """Para tiendas sin API pública: recorre el sitemap y lee los datos estructurados de cada producto."""
    out, vistos = [], set()
    for u in sitemap_urls(base, t.get("max_paginas", 400)):
        r = get(u)
        time.sleep(PAUSA / 2)
        if r is None or r.status_code != 200:
            continue
        for p in productos_ld(r.text)[:1]:
            offers = p.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            precio = offers.get("price") or offers.get("lowPrice")
            m = re.search(r"js-compare-price-display[^>]*>\s*([^<]+)<", r.text)
            lista = num_ar(m.group(1)) if m else None
            marca = p.get("brand")
            marca = marca.get("name") if isinstance(marca, dict) else marca
            img = p.get("image")
            img = img[0] if isinstance(img, list) and img else img
            sku = u.rstrip("/").split("/")[-1]
            if sku in vistos:
                continue
            vistos.add(sku)
            x = rec(t["id"], sku, marca, p.get("name"), precio, lista,
                    "InStock" in str(offers.get("availability", "")), u,
                    img if isinstance(img, str) else None, base)
            if x:
                out.append(x)
    return out


def detectar(base):
    r = get(f"{base}/api/catalog_system/pub/products/search?_from=0&_to=0")
    if r is not None and r.status_code in (200, 206) and r.text.strip().startswith("["):
        return "vtex"
    r = get(f"{base}/products.json?limit=1")
    if r is not None and r.status_code == 200 and '"products"' in r.text[:300]:
        return "shopify"
    return "generico"


ADAPTADORES = {"vtex": vtex, "shopify": shopify, "generico": generico}


def correr_tienda(t):
    base = t["url"].rstrip("/")
    plat = t.get("plataforma", "auto")
    if plat == "auto":
        plat = detectar(base)
    return plat, ADAPTADORES[plat](t, base)


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


# ---------- principal ----------
def cargar(path, defecto):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defecto


def main():
    diagnostico = "--diagnose" in sys.argv
    previo = cargar(DATA / "precios.json", {"productos": [], "actualizado": None})
    previos = {p["id"]: p for p in previo.get("productos", [])}
    hist = cargar(DATA / "historial.json", {})
    nombres = {t["id"]: t["nombre"] for t in CFG["tiendas"]}
    productos, estado = [], {}

    for t in CFG["tiendas"]:
        print(f"== {t['nombre']} ({t['url']})")
        try:
            plat, items = correr_tienda(t)
            err = None if items else "0 productos (¿cambió la web o bloquea el acceso?)"
        except Exception as e:  # una tienda rota no debe frenar a las demás
            plat, items, err = "?", [], f"{type(e).__name__}: {e}"
        con_desc = sum(1 for p in items if p["descuento"] > 0)
        print(f"   plataforma: {plat} | productos: {len(items)} | con descuento: {con_desc}" + (f" | ERROR: {err}" if err else ""))
        if diagnostico and items:
            for p in sorted(items, key=lambda p: -p["descuento"])[:3]:
                print(f"   ej: {p['nombre'][:50]} {plata(p['precio'])} lista={p['lista']} -{p['descuento']}% stock={p['stock']}")
        if not items:  # conservar lo último que se sabía de esa tienda
            items = [p for p in previos.values() if p["tienda"] == t["id"]]
        estado[t["id"]] = {"nombre": t["nombre"], "ok": err is None, "plataforma": plat,
                           "cantidad": len(items), "error": err}
        productos += items

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

    # ofertas nuevas: descuento alto y (producto nuevo, antes no era oferta, o bajó 10%+)
    umbral = CFG.get("umbral_aviso", 40)
    nuevas = []
    if previo.get("actualizado"):  # en la primera corrida no se avisa todo el catálogo
        for p in productos:
            if not p["stock"] or p["descuento"] < umbral:
                continue
            a = previos.get(p["id"])
            if a is None or a["descuento"] < umbral or p["precio"] <= a["precio"] * 0.9:
                nuevas.append(p)
    if nuevas:
        avisar(nuevas, nombres)
    print(f"Ofertas nuevas avisadas: {len(nuevas)}")

    salida = {"actualizado": ahora.isoformat(), "tiendas": estado, "productos": productos}
    (DATA / "precios.json").write_text(json.dumps(salida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA / "historial.json").write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
