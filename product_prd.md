# Product Requirements Document (PRD)

## Product: Single-Node Web Crawler + Search System

## 1. Objective
Build a single-machine web crawler that:
- Crawls web pages starting from a given origin URL up to depth `k`
- Avoids duplicate crawling globally
- Applies backpressure to control load
- Indexes content incrementally
- Supports real-time search over indexed pages while crawling is ongoing

## 2. Engineering Constraints
### Philosophy
Prefer language-native functionality and standard libraries. Avoid using third-party libraries that implement core logic out of the box.

### Rules
- Do NOT use:
  - Full crawler frameworks
  - Search engines (e.g., Elasticsearch)
  - Prebuilt inverted index libraries
  - External queue systems (e.g., Kafka, RabbitMQ)

- Allowed:
  - Basic HTTP clients
  - Lightweight HTML parsers
  - SQLite or simple storage

- Must implement manually:
  - BFS crawling logic
  - URL deduplication
  - Queue management
  - Backpressure mechanisms
  - Inverted index
  - Ranking logic

## 3. Assumptions
- Runs on a single machine
- Indexing starts before search
- Pages are static HTML (no JS rendering)
- Crawl scale is large but fits within one machine
- Search results update as indexing progresses

## 4. Core Features

### Index API
POST /index

Input:
{
  "origin": "string",
  "k": integer
}

### Search API
GET /search?query=...

Output:
[
  {
    "relevant_url": "...",
    "origin_url": "...",
    "depth": integer
  }
]

### Status API
GET /status

## 5. Crawling Strategy
- Breadth-First Search (BFS)
- Depth-limited traversal
- Global URL deduplication

## 6. Backpressure
- Max queue size
- Rate limiting
- Worker limits

States:
- NORMAL
- THROTTLED
- PAUSED

## 7. Search
- Keyword-based
- Incremental updates
- Simple ranking (term frequency + depth)

## 8. Storage
- SQLite for metadata
- In-memory index

## 9. Acceptance Criteria
- No duplicate crawling
- Search works during crawl
- Backpressure prevents overload

## 10. Implementation Plan
1. URL normalization
2. Crawler
3. Storage
4. Indexing
5. Search
6. Backpressure
