SERVICE_GRAPH: dict[str, list[str]] = {
    "edge-gateway": ["auth-service", "catalog-service", "cart-service", "checkout-service"],
    "auth-service": [],
    "catalog-service": ["redis-cache"],
    "cart-service": ["redis-cache"],
    "checkout-service": ["payment-service", "inventory-service", "order-service"],
    "payment-service": [],
    "inventory-service": ["order-db"],
    "order-service": ["order-db"],
    "redis-cache": [],
    "order-db": [],
}


def upstream_of(target: str) -> list[str]:
    return [name for name, deps in SERVICE_GRAPH.items() if target in deps]
