from __future__ import annotations

from datetime import datetime, timezone
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from thoth_terminal.connectors.document_fetch import build_fetched_document_payload
from thoth_terminal.net import FetchError, fetch_with_fallback
from thoth_terminal.text import clean_whitespace, extract_text_from_html


DEFAULT_REPORT_URL = "https://www.futureelectronics.com/resources/market-conditions-report/passives"
DEFAULT_PRODUCT_LIST_URL = (
    "https://www.futureelectronics.com/c/passives/products?q=%3Arelevance%3AinStockFlag%3Atrue&text="
)
FUTURE_BASE_URL = "https://www.futureelectronics.com"


def _abs_url(raw: str | None, *, base_url: str) -> str | None:
    value = str(raw or "").strip()
    if not value or value.lower().startswith("javascript:"):
        return None
    return urljoin(base_url, value)


def _clean_label(value: str | None) -> str:
    text = clean_whitespace(str(value or "").replace("\xa0", " "))
    return text.strip()


def _number_from_text(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"[-+]?\d[\d,]*", raw)
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _float_from_text(value: str | None) -> float | None:
    raw = str(value or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_ordinal_date(value: str | None) -> datetime | None:
    text = _clean_label(value)
    if not text:
        return None
    text = re.sub(r"(\d{1,2})\s*(st|nd|rd|th)", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"^Last updated:\s*", "", text, flags=re.IGNORECASE).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _input_value(node, *names: str) -> str | None:
    for name in names:
        found = node.find("input", {"name": name})
        if found and found.get("value") is not None:
            return str(found.get("value") or "").strip()
    return None


def _input_int(node, *names: str) -> int | None:
    return _number_from_text(_input_value(node, *names))


def _input_price_breaks(node) -> list[dict[str, Any]]:
    count = _input_int(node, "numberOfPrices") or 0
    rows: list[dict[str, Any]] = []
    for idx in range(max(0, count)):
        qty = _input_int(node, f"volumePriceMinQty{idx}")
        price = _float_from_text(_input_value(node, f"volumePrice{idx}"))
        if qty is None and price is None:
            continue
        rows.append({"min_qty": qty, "unit_price": price, "currency": "USD"})
    if rows:
        return rows

    price_cell = node.find("td", class_="product_price")
    if not price_cell:
        return rows
    tokens = [_clean_label(t) for t in price_cell.stripped_strings]
    tokens = [t for t in tokens if t and t.lower() not in {"qty", "unit price"}]
    idx = 0
    while idx + 1 < len(tokens):
        qty = _number_from_text(tokens[idx])
        price = _float_from_text(tokens[idx + 1])
        if qty is not None or price is not None:
            rows.append({"min_qty": qty, "unit_price": price, "currency": "USD"})
        idx += 2
    return rows


def _page_url(list_url: str, *, page: int, page_size: int) -> str:
    parsed = urlparse(list_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["pageSize"] = str(max(1, int(page_size)))
    query["page"] = str(max(0, int(page)))
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            "",
        )
    )


def _pagination_stats(soup: BeautifulSoup) -> dict[str, int | None]:
    text = soup.get_text("\n", strip=True)
    total_results = None
    match = re.search(r"Results:\s*([\d,]+)", text, flags=re.IGNORECASE)
    if match:
        total_results = _number_from_text(match.group(1))

    total_pages = None
    for a in soup.select(".pagination a[href*='page=']"):
        href = str(a.get("href") or "")
        try:
            query = dict(parse_qsl(urlparse(href).query, keep_blank_values=True))
            page = int(query.get("page") or 0)
        except Exception:
            continue
        total_pages = max(total_pages or 0, page + 1)

    return {"total_results": total_results, "total_pages": total_pages}


def parse_market_report_html(html: str, *, report_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    report_title = _clean_label(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
    report_title = report_title or "Future Electronics Market Conditions Report"

    pdf_url = None
    shop_url = None
    category_links: list[dict[str, str]] = []
    other_product_links: list[dict[str, str]] = []

    for a in soup.find_all("a", href=True):
        text = _clean_label(a.get_text(" ", strip=True))
        href = _abs_url(a.get("href"), base_url=report_url)
        if not href:
            continue
        lowered_text = text.lower()
        if "view pdf" in lowered_text or href.lower().endswith(".pdf"):
            pdf_url = href
        elif "shop all" in lowered_text:
            shop_url = href
        elif "/c/passives" in href:
            category_links.append({"label": text or href, "url": href})
        elif "/resources/market-conditions-report/" in href and href != report_url:
            other_product_links.append({"label": text or href, "url": href})

    seen: set[str] = set()
    category_links = [row for row in category_links if not (row["url"] in seen or seen.add(row["url"]))]
    seen.clear()
    other_product_links = [row for row in other_product_links if not (row["url"] in seen or seen.add(row["url"]))]

    last_updated = None
    for text in soup.stripped_strings:
        if "last updated" in str(text).lower():
            parent_text = ""
            node = soup.find(string=lambda s: s == text)
            if node and getattr(node, "parent", None):
                parent_text = node.parent.get_text(" ", strip=True)
            last_updated = _parse_ordinal_date(parent_text or str(text))
            if last_updated:
                break

    image_urls = []
    for img in soup.find_all("img"):
        alt = _clean_label(img.get("alt"))
        src = _abs_url(img.get("src"), base_url=report_url)
        if not src:
            continue
        if "market" in alt.lower() or "mcr" in alt.lower() or "market" in src.lower():
            image_urls.append(src)

    return {
        "title": report_title,
        "last_updated": last_updated,
        "pdf_url": pdf_url,
        "shop_url": shop_url,
        "category_links": category_links,
        "other_product_links": other_product_links,
        "image_urls": image_urls[:5],
        "page_text": extract_text_from_html(html, url=report_url),
    }


def parse_product_list_html(html: str, *, page_url: str, page: int | None = None) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    stats = _pagination_stats(soup)
    products: list[dict[str, Any]] = []

    for idx, row in enumerate(soup.select("tr.list-row")):
        item = row.select_one(".product__list--item") or row
        code = _input_value(item, "productCodePost", "id")
        mpn = _input_value(item, "productPostMpn", "name")
        product_anchor = item.select_one("a.product__list--code[href]") or item.select_one("a[href*='/p/']")
        product_url = _abs_url(product_anchor.get("href") if product_anchor else None, base_url=page_url)
        if not mpn and product_anchor:
            mpn = _clean_label(product_anchor.get_text(" ", strip=True))

        manufacturer = _input_value(item, "productPostManufacturer", "dimension6")
        if not manufacturer:
            manufacturer_node = item.select_one(".product__list--name")
            manufacturer = _clean_label(manufacturer_node.get_text(" ", strip=True) if manufacturer_node else "")
        description = _input_value(item, "description")
        if not description:
            desc_node = item.select_one(".product__list--description")
            description = _clean_label(desc_node.get_text(" ", strip=True) if desc_node else "")

        datasheet_url = None
        datasheet = item.select_one("a.btn--datasheet[href]") or item.find("a", string=re.compile("Datasheet", re.I))
        if datasheet:
            datasheet_url = _abs_url(datasheet.get("href"), base_url=page_url)

        product_cells = row.find_all("td", recursive=False)
        stock_cell = row.find("td", class_="product_stock")
        package_cells = row.find_all("td", class_="product_pkg")

        product = {
            "future_product_code": code,
            "manufacturer_part_number": mpn,
            "manufacturer": manufacturer,
            "manufacturer_code": _input_value(item, "productPostBrand", "brand"),
            "description": description,
            "category": _input_value(item, "productPostCategory", "category"),
            "variant": _input_value(item, "productPostVariant", "variant"),
            "price_type": _input_value(item, "productPostPriceType", "dimension7"),
            "stock_status": _input_value(item, "productPostStock", "dimension11"),
            "inventory_type": _input_value(item, "dimension12"),
            "product_url": product_url,
            "datasheet_url": datasheet_url,
            "image_url": _input_value(item, "imageURL"),
            "minimum_order_quantity": _number_from_text(
                item.select_one(".product-min").get_text(" ", strip=True)
                if item.select_one(".product-min")
                else None
            )
            or _input_int(item, "qty"),
            "multiple_quantity": _number_from_text(
                item.select_one(".product-mult").get_text(" ", strip=True)
                if item.select_one(".product-mult")
                else None
            ),
            "price_breaks": _input_price_breaks(item),
            "global_stock": _number_from_text(stock_cell.get_text(" ", strip=True) if stock_cell else None),
            "availability_text": _clean_label(stock_cell.get_text(" ", strip=True) if stock_cell else ""),
            "lead_time": _clean_label(package_cells[0].get_text(" ", strip=True)) if len(package_cells) > 0 else None,
            "date_code": _clean_label(package_cells[1].get_text(" ", strip=True)) if len(package_cells) > 1 else None,
            "package": _clean_label(package_cells[2].get_text(" ", strip=True)) if len(package_cells) > 2 else None,
            "tariff_indicator": bool(item.select_one(".product_list--isTariffApply")),
            "page": page,
            "position": _input_int(item, "position") or (idx + 1),
            "source_page_url": page_url,
        }
        if product["manufacturer_part_number"] or product["future_product_code"] or product["product_url"]:
            product["raw_row_text"] = _clean_label(row.get_text(" ", strip=True))
            product["product_cells"] = [_clean_label(c.get_text(" ", strip=True)) for c in product_cells]
            products.append(product)

    return {"products": products, "stats": stats}


def _table_pairs(table) -> dict[str, str]:
    pairs: dict[str, str] = {}
    if not table:
        return pairs
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        key = _clean_label(cells[0].get_text(" ", strip=True)).rstrip(":")
        value = _clean_label(cells[1].get_text(" ", strip=True))
        if key and value:
            pairs[key] = value
    return pairs


def parse_product_detail_html(html: str, *, product_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    h1 = soup.find("h1")
    h2 = soup.find("h2")
    title = _clean_label(h1.get_text(" ", strip=True) if h1 else "")
    subtitle = _clean_label(h2.get_text(" ", strip=True) if h2 else "")

    spec_rows: dict[str, str] = {}
    specs = soup.select_one("#packaging-section-tb")
    if specs:
        for row in specs.select(".fe-row.btm-line"):
            left = row.select_one(".left-sec")
            right = row.select_one(".right-sec")
            key = _clean_label(left.get_text(" ", strip=True) if left else "").rstrip(":")
            value = _clean_label(right.get_text(" ", strip=True) if right else "")
            if key and value:
                spec_rows[key] = value

    attributes = _table_pairs(soup.select_one(".attribute-section table"))

    features_text = ""
    feature_heading = soup.find(string=lambda s: s and "Features & Applications" in str(s))
    if feature_heading:
        section = feature_heading.parent
        for _ in range(4):
            if section and section.name != "div":
                section = section.parent
        if section:
            features_text = _clean_label(section.get_text(" ", strip=True))

    packaging: dict[str, str] = {}
    packaging_block = soup.select_one(".packaging-block")
    if packaging_block:
        for h5 in packaging_block.find_all("h5"):
            key = _clean_label(h5.get_text(" ", strip=True)).rstrip(":")
            value_parts: list[str] = []
            for sibling in h5.next_siblings:
                if getattr(sibling, "name", None) == "h5":
                    break
                if hasattr(sibling, "get_text"):
                    value_parts.append(_clean_label(sibling.get_text(" ", strip=True)))
                elif str(sibling).strip():
                    value_parts.append(_clean_label(str(sibling)))
            value = _clean_label(" ".join(value_parts))
            if key and value:
                packaging[key] = value

    images = []
    for img in soup.find_all("img"):
        src = _abs_url(img.get("src"), base_url=product_url)
        alt = _clean_label(img.get("alt") or img.get("title"))
        if src and ("/passives/" in src or alt):
            images.append({"url": src, "alt": alt})

    datasheets = []
    for a in soup.find_all("a", href=True):
        text = _clean_label(a.get_text(" ", strip=True))
        href = _abs_url(a.get("href"), base_url=product_url)
        if href and "datasheet" in text.lower():
            datasheets.append({"label": text or "Datasheet", "url": href})

    return {
        "title": title,
        "subtitle": subtitle,
        "product_specification": spec_rows,
        "technical_attributes": attributes,
        "packaging": packaging,
        "features_and_applications": features_text,
        "image_assets": images[:8],
        "datasheets": datasheets[:8],
    }


def _product_title(product: dict[str, Any]) -> str:
    mpn = str(product.get("manufacturer_part_number") or "").strip()
    manufacturer = str(product.get("manufacturer") or "").strip()
    if manufacturer and mpn:
        return f"Future Electronics product: {manufacturer} {mpn}"
    return f"Future Electronics product: {mpn or product.get('future_product_code') or product.get('product_url')}"


def _product_text(product: dict[str, Any]) -> str:
    lines = [
        _product_title(product),
        f"Description: {product.get('description') or ''}",
        f"Category: {product.get('category') or ''}",
        f"Variant: {product.get('variant') or ''}",
        f"Stock: {product.get('global_stock') or ''} ({product.get('stock_status') or product.get('availability_text') or ''})",
        f"Lead time: {product.get('lead_time') or ''}",
        f"Date code: {product.get('date_code') or ''}",
        f"Package: {product.get('package') or ''}",
        f"Minimum order quantity: {product.get('minimum_order_quantity') or ''}",
        f"Multiple quantity: {product.get('multiple_quantity') or ''}",
    ]
    if product.get("price_breaks"):
        lines.append("Price breaks:")
        for row in product.get("price_breaks") or []:
            lines.append(
                f"- {row.get('min_qty') or ''} units: ${row.get('unit_price') if row.get('unit_price') is not None else ''}"
            )
    attrs = product.get("technical_attributes") or {}
    if attrs:
        lines.append("Technical attributes:")
        for key, value in attrs.items():
            lines.append(f"- {key}: {value}")
    specs = product.get("product_specification") or {}
    if specs:
        lines.append("Product specification:")
        for key, value in specs.items():
            lines.append(f"- {key}: {value}")
    features = str(product.get("features_and_applications") or "").strip()
    if features:
        lines.append("Features and applications:")
        lines.append(features)
    return clean_whitespace("\n".join(line for line in lines if line is not None))


def _market_report_item(report: dict[str, Any], *, report_url: str, pdf_payload: dict[str, Any] | None) -> dict[str, Any]:
    last_updated = report.get("last_updated")
    title_suffix = ""
    if isinstance(last_updated, datetime):
        title_suffix = f" (updated {last_updated.date().isoformat()})"
    title = f"Future Electronics Market Conditions Report: {report.get('title') or 'Passives'}{title_suffix}"

    pdf_text = str((pdf_payload or {}).get("normalized_text") or "")
    page_text = str(report.get("page_text") or "")
    body = clean_whitespace("\n\n".join(part for part in [page_text, pdf_text] if part))
    if not body:
        body = title

    attachments = list((pdf_payload or {}).get("attachments") or [])
    image_urls = list(report.get("image_urls") or [])
    links = [report_url]
    for key in ("pdf_url", "shop_url"):
        if report.get(key):
            links.append(str(report[key]))
    for row in list(report.get("category_links") or []) + list(report.get("other_product_links") or []):
        if isinstance(row, dict) and row.get("url"):
            links.append(str(row["url"]))

    return {
        "kind": "market_report",
        "external_id": "futureelectronics:market-report:passives",
        "url": report_url,
        "canonical_url": report_url,
        "title": title,
        "author": "Future Electronics",
        "published_at": last_updated if isinstance(last_updated, datetime) else None,
        "raw_text": body,
        "normalized_text": body,
        "image_urls": image_urls,
        "attachments": attachments,
        "links": links,
        "extra": {
            "connector": "future_electronics",
            "source_kind": "future_electronics_market_report",
            "report_url": report_url,
            "report_pdf_url": report.get("pdf_url"),
            "shop_url": report.get("shop_url"),
            "report_last_updated": last_updated.isoformat() if isinstance(last_updated, datetime) else None,
            "category_links": report.get("category_links") or [],
            "other_product_links": report.get("other_product_links") or [],
            "pdf_extracted": bool(pdf_text),
            **dict((pdf_payload or {}).get("extra") or {}),
        },
    }


def _product_item(product: dict[str, Any], *, fetched_at: datetime) -> dict[str, Any]:
    product_url = str(product.get("product_url") or "").strip() or None
    code = str(product.get("future_product_code") or "").strip()
    mpn = str(product.get("manufacturer_part_number") or "").strip()
    external_id = f"futureelectronics:product:{code or mpn or product_url}"
    image_urls = [str(product["image_url"])] if product.get("image_url") else []
    for row in product.get("image_assets") or []:
        if isinstance(row, dict) and row.get("url"):
            image_urls.append(str(row["url"]))
    links = [u for u in [product_url, product.get("datasheet_url")] if u]
    for row in product.get("datasheets") or []:
        if isinstance(row, dict) and row.get("url"):
            links.append(str(row["url"]))
    seen_links: set[str] = set()
    links = [str(u) for u in links if str(u) and not (str(u) in seen_links or seen_links.add(str(u)))]

    text = _product_text(product)
    return {
        "kind": "product_snapshot",
        "external_id": external_id,
        "url": product_url,
        "canonical_url": product_url,
        "title": _product_title(product),
        "author": "Future Electronics",
        "published_at": fetched_at,
        "raw_text": text,
        "normalized_text": text,
        "image_urls": image_urls[:8],
        "links": links,
        "extra": {
            "connector": "future_electronics",
            "source_kind": "future_electronics_product_snapshot",
            "fetched_at": fetched_at.isoformat(),
            "product": product,
        },
    }


def fetch_future_electronics(
    config: dict[str, Any],
    *,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    report_url = str(cfg.get("report_url") or cfg.get("list_url") or DEFAULT_REPORT_URL).strip()
    product_list_url = str(cfg.get("product_list_url") or DEFAULT_PRODUCT_LIST_URL).strip()
    page_size = max(1, min(int(cfg.get("product_page_size") or cfg.get("page_size") or 100), 100))
    max_pages = max(0, int(cfg.get("max_product_pages_per_run") or 3))
    max_detail_raw = cfg.get("max_detail_products_per_run")
    max_detail = max(0, int(25 if max_detail_raw is None or str(max_detail_raw).strip() == "" else max_detail_raw))
    fetch_details = bool(cfg.get("fetch_product_details", True))
    include_front_page = bool(cfg.get("include_front_product_page_each_run", True))
    emit_product_documents = bool(cfg.get("emit_product_documents", False))
    delay_s = max(0.0, float(cfg.get("request_delay_seconds") or 0.0))
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)

    items: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "source_kind": "future_electronics",
        "report_url": report_url,
        "product_list_url": product_list_url,
        "product_page_size": page_size,
        "product_pages_requested": max_pages,
        "products_seen": 0,
        "product_details_fetched": 0,
        "fetched_at": fetched_at.isoformat(),
    }

    report_res = fetch_with_fallback(report_url, timeout_s=float(cfg.get("report_timeout_s") or 30.0))
    report = parse_market_report_html(report_res.text or "", report_url=report_res.url or report_url)
    pdf_payload = None
    if cfg.get("download_report_pdf", True) and report.get("pdf_url"):
        try:
            pdf_res = fetch_with_fallback(str(report["pdf_url"]), timeout_s=float(cfg.get("pdf_timeout_s") or 45.0))
            pdf_payload = build_fetched_document_payload(request_url=str(report["pdf_url"]), result=pdf_res)
        except FetchError as exc:
            stats["pdf_error"] = f"{type(exc).__name__}: {exc}"
    items.append(_market_report_item(report, report_url=report_url, pdf_payload=pdf_payload))

    next_page = max(0, int(cursor.get("next_product_page") or 0))
    pages: list[int] = []
    if include_front_page:
        pages.append(0)
    for page in range(next_page, next_page + max_pages):
        if page not in pages:
            pages.append(page)

    total_pages = None
    products: list[dict[str, Any]] = []
    for page in pages:
        page_url = _page_url(product_list_url, page=page, page_size=page_size)
        try:
            if delay_s:
                time.sleep(delay_s)
            page_res = fetch_with_fallback(page_url, timeout_s=float(cfg.get("product_timeout_s") or 35.0))
        except FetchError as exc:
            stats.setdefault("product_page_errors", []).append({"page": page, "error": f"{type(exc).__name__}: {exc}"})
            continue
        parsed = parse_product_list_html(page_res.text or "", page_url=page_res.url or page_url, page=page)
        page_stats = parsed.get("stats") or {}
        if page_stats.get("total_pages"):
            total_pages = int(page_stats["total_pages"])
        if page_stats.get("total_results"):
            stats["total_results"] = int(page_stats["total_results"])
        products.extend(list(parsed.get("products") or []))

    seen_products: set[str] = set()
    deduped_products: list[dict[str, Any]] = []
    for product in products:
        key = str(product.get("future_product_code") or product.get("manufacturer_part_number") or product.get("product_url") or "")
        if key and key in seen_products:
            continue
        if key:
            seen_products.add(key)
        deduped_products.append(product)

    stats["products_seen"] = len(deduped_products)
    for product in deduped_products:
        product["fetched_at"] = fetched_at.isoformat()
    detail_count = 0
    if fetch_details and max_detail > 0:
        for product in deduped_products:
            if detail_count >= max_detail:
                break
            product_url = str(product.get("product_url") or "").strip()
            if not product_url:
                continue
            try:
                if delay_s:
                    time.sleep(delay_s)
                detail_res = fetch_with_fallback(product_url, timeout_s=float(cfg.get("detail_timeout_s") or 35.0))
                detail = parse_product_detail_html(detail_res.text or "", product_url=detail_res.url or product_url)
                product.update(detail)
                detail_count += 1
            except FetchError as exc:
                product["detail_error"] = f"{type(exc).__name__}: {exc}"
        stats["product_details_fetched"] = detail_count

    stats["product_snapshots"] = deduped_products
    if emit_product_documents:
        items.extend(_product_item(product, fetched_at=fetched_at) for product in deduped_products)

    if total_pages is None:
        total_pages = int(cursor.get("total_product_pages") or 0) or None
    next_cursor_page = next_page + max_pages
    if total_pages and next_cursor_page >= total_pages:
        next_cursor_page = 0

    next_cursor = {
        "next_product_page": max(0, next_cursor_page),
        "total_product_pages": total_pages,
        "last_product_page_size": page_size,
        "last_fetched_at": fetched_at.isoformat(),
    }
    stats["total_pages"] = total_pages
    stats["next_product_page"] = next_cursor_page
    return {"items": items, "cursor": next_cursor, "stats": stats}
