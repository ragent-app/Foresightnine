"""News discovery modules (plan W5): GDELT client, publication-date extraction, duplicate clustering, merged search.

These modules are standalone; the research runtime wires them in separately.
"""
from .clustering import ClusterInput, ClusterResult, NewsCluster, canonical_news_url, cluster_items
from .gdelt import GDELT_ATTRIBUTION, GdeltClient, NewsLead
from .published_date import DateFound, PublishedDateResult, extract_published_date
from .search import GdeltNewsProvider, GlmNewsProvider, news_search

__all__ = ["ClusterInput", "ClusterResult", "NewsCluster", "canonical_news_url", "cluster_items", "GDELT_ATTRIBUTION",
           "GdeltClient", "NewsLead", "DateFound", "PublishedDateResult", "extract_published_date",
           "GdeltNewsProvider", "GlmNewsProvider", "news_search"]
