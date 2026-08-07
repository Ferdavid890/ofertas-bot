def buscar_auctions_hoy(headers, stats):
    """Obtiene todas las subastas activas de Lorcana PSA 10 para filtrarlas localmente por fecha."""
    items_acumulados = []
    limit = 100
    
    # Quitamos el filtro de endTime restrictivo y pedimos todas las subastas activas de este producto
    url_base = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=buyingOptions:AUCTION&limit={limit}&offset="
    
    stats["consultas_ebay"] += 1
    resp = peticion_ebay_con_retry(url_base + "0", headers)
    if not resp or resp.status_code != 200:
        logging.error(f"[Error API Auctions] No se pudo obtener respuesta: {resp.text if resp else 'Sin conexión'}")
        return items_acumulados

    data = resp.json()
    total = data.get("total", 0)
    items_acumulados.extend(data.get("itemSummaries", []))
    
    logging.info(f"[Debug Auctions] Total de subastas encontradas en eBay US: {total}")
    
    paginas_totales = ceil(total / limit) if total > 0 else 1
    for page in range(1, min(paginas_totales, 15)): # Límite seguro de páginas
        offset = page * limit
        stats["consultas_ebay"] += 1
        resp = peticion_ebay_con_retry(url_base + str(offset), headers)
        if resp and resp.status_code == 200:
            items = resp.json().get("itemSummaries", [])
            if not items:
                break
            items_acumulados.extend(items)
        else:
            break
            
    return items_acumulados
