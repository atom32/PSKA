from __future__ import annotations

from pathlib import Path

from pska_core.knowledge_sources import KnowledgeSourceService
from pska_core.source_adapters import build_source_adapter
from pska_core.store import InMemoryKnowledgeStore


def test_rss_source_adapter_previews_and_syncs_feed_items(tmp_path: Path) -> None:
    feed = tmp_path / "feed.xml"
    feed.write_text(
        """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>PSKA Updates</title>
    <item>
      <title>Digest landed</title>
      <link>https://example.test/digest</link>
      <guid>digest-1</guid>
      <description><![CDATA[<p>Digest candidates now carry source refs.</p>]]></description>
      <pubDate>Mon, 29 Jun 2026 05:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
""",
        encoding="utf-8",
    )
    store = InMemoryKnowledgeStore()
    service = KnowledgeSourceService(store)
    source = service.add_rss_source(feed.resolve().as_uri())
    adapter = build_source_adapter(store, source)

    preview = adapter.preview()
    report = adapter.sync()
    run = service.record_sync_report(source, report)
    spans = store.list_processing_spans(sync_run_id=run.sync_run_id)

    assert preview["count"] == 1
    assert report.scanned == 1
    assert report.ingested == 1
    item = store.source_items[report.source_item_ids[0]]
    assert item.source_channel == "rss"
    assert item.title == "Digest landed"
    assert "source refs" in item.content_text
    assert {span.stage for span in spans} == {"discover", "extract", "chunk", "embed", "index", "digest"}


def test_url_source_adapter_extracts_html_and_ignores_scripts(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text(
        """<!doctype html>
<html>
  <head><title>Adapter Page</title><script>secret()</script></head>
  <body><main><h1>Readable body</h1><p>URL pages become knowledge sources.</p></main></body>
</html>
""",
        encoding="utf-8",
    )
    store = InMemoryKnowledgeStore()
    source = KnowledgeSourceService(store).add_url_source(page.resolve().as_uri())
    report = build_source_adapter(store, source).sync()

    assert report.scanned == 1
    assert report.ingested == 1
    item = store.source_items[report.source_item_ids[0]]
    assert item.source_channel == "url"
    assert item.title == "Adapter Page"
    assert "Readable body" in item.content_text
    assert "secret()" not in item.content_text


def test_url_source_adapter_expands_sitemap(tmp_path: Path) -> None:
    page = tmp_path / "first.html"
    page.write_text("<html><head><title>First</title></head><body>First body</body></html>", encoding="utf-8")
    sitemap = tmp_path / "sitemap.xml"
    sitemap.write_text(
        f"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{page.resolve().as_uri()}</loc></url>
</urlset>
""",
        encoding="utf-8",
    )
    store = InMemoryKnowledgeStore()
    source = KnowledgeSourceService(store).add_url_source(sitemap.resolve().as_uri())

    preview = build_source_adapter(store, source).preview()
    report = build_source_adapter(store, source).sync()

    assert preview["count"] == 1
    assert report.ingested == 1
    assert store.source_items[report.source_item_ids[0]].title == "First"
