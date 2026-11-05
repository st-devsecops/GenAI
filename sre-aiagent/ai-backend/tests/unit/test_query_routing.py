import unittest
from datetime import datetime, timezone
from app.rag.query import classify_query, route_query, parse_time_window, resolve_environment

class QueryRoutingTests(unittest.TestCase):
    def test_explicit_environment_narrows_default_all(self):
        for query, expected in (
            ("Show me the CPU and memory of the tfs squid stg in hub env", "hub"),
            ("Show CPU in the HUB environment", "hub"),
            ("Show CPU environment=prod", "prod"),
            ("Show CPU in stg", "stg"),
            ("Show CPU for the prod environment", "prod"),
            ("Show CPU for tfs-squid-stg", "all"),
            ("Show CPU for host.stg.example.net", "all"),
            ("Show CPU in hub.example.net", "all"),
            ("Show CPU in hub and prod", "all"),
            ("Show CPU across stg, prod and hub", "all"),
            ("Show CPU in hub and in stg", "all"),
            ("Show CPU and memory", "all"),
        ):
            with self.subTest(query=query):
                self.assertEqual(resolve_environment(query), expected)
        self.assertEqual(resolve_environment("Show CPU in hub env", "stg"), "stg")

    def test_metrics_take_priority_over_incidental_keyword_words(self):
        self.assertEqual(route_query('Show CPU and memory of elastic search pods')['query_sources'], ['grafana'])
        self.assertEqual(route_query('Show CPU metrics matching pod elasticsearch')['search_mode'], 'metrics')

    def test_keyword_and_semantic_routes(self):
        self.assertEqual(route_query('Find logs containing connection refused')['query_sources'], ['elasticsearch'])
        self.assertEqual(route_query('Find incidents similar to connection refused')['query_sources'], ['qdrant'])

    def test_kubernetes_resource_requests_route_to_read_only_mcp(self):
        for query in ('List pods in namespace sre-ai', 'Describe deployment sre-ai-agent', 'Show Kubernetes events', 'Show me the image pull errors', 'List ImagePullBackOff pods'):
            with self.subTest(query=query):
                result = route_query(query)
                self.assertEqual(result['query_classification'], 'kubernetes')
                self.assertEqual(result['query_intents'], ['KUBERNETES_QUERY'])
                self.assertEqual(result['query_sources'], ['kubernetes'])
        self.assertIn('kubernetes', route_query('Show pod logs')['query_sources'])
        self.assertEqual(route_query('Show CPU and memory of Elasticsearch pod')['query_sources'], ['prometheus'])
        self.assertIn('kubernetes', route_query('Why are pods in ImagePullBackOff?')['query_sources'])

    def test_mixed_rca_routes_to_all_sources(self):
        self.assertEqual(route_query('Why did CPU increase and what errors occurred?')['query_sources'], ['elasticsearch', 'qdrant', 'grafana'])

    def test_relative_ranges_ignore_invalid_timezone(self):
        now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        start, end = parse_time_window(time_range='last 5 days', timezone_name='Invalid/BrowserZone', max_hours=168, now=now)
        self.assertEqual((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 5 * 86400)

    def test_minutes_and_week_ranges(self):
        now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        start, end = parse_time_window(time_range='last 30 minutes', max_hours=168, now=now)
        self.assertEqual((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1800)
        start, end = parse_time_window(time_range='last 1 week', max_hours=168, now=now)
        self.assertEqual((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 7 * 86400)
        start, end = parse_time_window(time_range='last 30 days', max_hours=720, now=now)
        self.assertEqual((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 30 * 86400)

if __name__ == '__main__':
    unittest.main()
