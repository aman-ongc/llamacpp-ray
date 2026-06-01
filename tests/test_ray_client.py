from gateway.ray_client import _affinity_proxy_url, _proxy_urls, _select_proxy_url


def test_affinity_is_deterministic():
    key = "sk-ongc-abc123"
    assert _affinity_proxy_url(key) == _affinity_proxy_url(key)


def test_affinity_maps_to_known_proxy():
    key = "sk-ongc-abc123"
    assert _affinity_proxy_url(key) in _proxy_urls


def test_different_keys_can_map_to_different_proxies():
    urls = {_affinity_proxy_url(f"sk-ongc-user{i}") for i in range(20)}
    # With 4 nodes and 20 different keys, expect at least 2 distinct targets.
    assert len(urls) >= 2


def test_select_proxy_url_with_affinity_key_is_deterministic():
    key = "sk-ongc-xyz"
    assert _select_proxy_url(key) == _select_proxy_url(key)


def test_select_proxy_url_without_key_returns_valid_url():
    url = _select_proxy_url(None)
    assert any(url.startswith(f"http://{p.split('://')[-1].split(':')[0]}") for p in _proxy_urls)
