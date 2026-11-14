# Ofertas de perfumes

Busca precios cada 3 horas en Juleriaque, Rouge, La Parfumerie y Ruiz y Roca, te avisa por Telegram (o ntfy) cuando aparece una oferta nueva y las muestra en una página.

```
GitHub Actions (cada 3 h) → scraper/scrape.py → data/precios.json → aviso al celular
                                                      ↓
                                               index.html (GitHub Pages)
```

## Puesta en marcha

1. **Repo:** creá un repo en GitHub (por ejemplo `ofertas-perfumes`) y subí todo el contenido de esta carpeta, incluida `.github/`.
2. **Página:** Settings → Pages → Deploy from a branch → `main` / `(root)`. Tu página queda en `https://TU_USUARIO.github.io/ofertas-perfumes/`.
3. **Permiso para guardar datos:** Settings → Actions → General → Workflow permissions → *Read and write permissions*.
4. **Avisos por Telegram:**
   - En Telegram hablale a **@BotFather**, mandá `/newbot` y copiá el token.
   - Escribile cualquier mensaje a tu bot nuevo y abrí `https://api.telegram.org/botTU_TOKEN/getUpdates`: ahí aparece tu `chat` → `id`.
   - En el repo: Settings → Secrets and variables → Actions → *New repository secret*: `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.
   - *(Alternativa sin cuenta)* instalá la app **ntfy**, suscribite a un tema difícil de adivinar y guardalo como secret `NTFY_TOPIC`.
   - *(Opcional)* en la pestaña Variables creá `SITE_URL` con la dirección de tu página, así el aviso trae el link.
5. **Primera corrida:** pestaña Actions → "Buscar ofertas" → Run workflow. La primera vez no avisa nada (solo carga el catálogo); desde la segunda avisa lo nuevo.

## Probar en tu compu antes de subir

```bash
pip3 install requests
python3 scraper/scrape.py --diagnose
```

Muestra, por tienda, qué plataforma detectó, cuántos productos leyó y ejemplos con precio y descuento. Si una tienda dice `0 productos`, mirá abajo.

## Si una tienda no anda

- **Ruiz y Roca:** la dirección en `config.json` es una suposición. Verificala y corregila.
- El scraper detecta solo si la tienda es VTEX o Shopify (leen todo el catálogo por API). Si no es ninguna, usa el modo `generico`: lee el sitemap y los datos estructurados de cada producto (máx. 400 páginas por corrida, configurable con `"max_paginas"`). En ese modo puede que no encuentre el precio de lista y no calcule descuento.
- Podés forzar la plataforma con `"plataforma": "vtex"`, `"shopify"` o `"generico"`.
- Si una tienda falla, se conservan sus últimos datos y la página muestra un punto rojo.

## Ajustes

- `umbral_aviso` en `config.json`: descuento mínimo para avisar (40 por defecto).
- Frecuencia: línea `cron` de `.github/workflows/buscar-ofertas.yml` (la hora es UTC).
- Agregar una tienda: sumá un bloque en `tiendas`.

## Notas

- Es para uso personal. Respetá los términos de cada tienda y no bajes `PAUSA` en el scraper.
- El agrupado de "Comparar tiendas" se hace por nombre y tamaño, así que es aproximado.
- Si el repo pasa mucho tiempo sin actividad, GitHub puede pausar las corridas programadas: entrá a Actions y reactivalas.
