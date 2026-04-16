
---
title: Entity Resolution in Snowflake
date: 2026-04-15
description: Using Cortex Search, LLM query variants, and reciprocal rank fusion to find duplicate item records.
---

Entity resolution is all about determining whether different data records are actually referring to the same real world entity.

For example:

```text
IPHONE_13_PRO_128GB_BLACK
iPhone 13 Pro Black 128 GB
Apple iPhone13 Pro 128G - Black
APL IPH13P 128GB BK
```

In this case, it is pretty obvious to a person that all of these are referring to the same exact phone model and spec. The words are arranged differently, some terms are abbreviated, and the formatting is inconsistent, but the meaning is still clear.

However, real business data is usually not this friendly.

The harder cases show up when the names contain domain specific abbreviations, internal shorthand, vendor slang, or old naming conventions that only make sense if you have spent enough time around the data.

Take this example where we are describing apparel names from The North Face brand:

```text
TNF BC HDY BLK L
North Face Base Camp Hoodie Black Large
NF Basecamp Pullover BLK L
TNF BC PO Black LRG
```

All of these can refer to the same underlying product. But you only know that if you understand the mappings:

- TNF / NF / North Face
- BC / Base Camp / Basecamp
- HDY / Hoodie / Pullover / PO
- BLK / Black
- L / Large / LRG

This is where entity resolution becomes much more interesting. We have to move past finding strings that only look similar to ones that also mean the same thing even when the text itself looks very different.

This is the exact class of problem I ran into while trying to identify duplicate manufacturing items. Some products had multiple records that were clearly the same item once you understood the abbreviations, but those abbreviations were not obvious to a general purpose search system.

So in this post, I want to walk through how I approached the problem, starting from the naive solution and building up to a hybrid Snowflake architecture using Cortex Search, LLM generated query variants, deduplication, and reciprocal rank fusion.

## The Naive Approach

Naively, we can start off pretty simple.

If we want to know whether an item has duplicate records, we can run a SQL statement against our database:

```sql
SELECT item_name, COUNT(*)
FROM ITEMS
GROUP BY item_name
HAVING COUNT(*) > 1;
```

This catches exact duplicates.

And exact duplicates are worth catching, but it should not take long before you realize that life is not so easy.

For example, this query will catch:

```text
TNF BC HDY BLK L
TNF BC HDY BLK L
```

But it will completely miss:

```text
TNF BC HDY BLK L
North Face Base Camp Hoodie Black Large
```

Those two strings are not equal, even though they may represent the same exact thing.

So exact matching gets us started, but it is not going to get us very far.

## Better Approach: Near Duplicates

If you have ever tried the exact match approach and were left unsatisfied, you probably ended up here...fuzzy matching.

Fuzzy matching can be helpful when records have small variations. Algorithms like Levenshtein Distance and Jaccard Similarity can estimate how close two pieces of text are based on edits, overlapping tokens, or character level similarity.

Take the North Face example again:

```python
northface_skus = [
    # Same exact item
    "The North Face BASECAMP PO HOOD L",
    "TNF BC HDY BLK L",

    # Very similar item, but should NOT match
    "TNF BC FULLZIP HDY BLK L",
    "NF BASE CAMP ZIP HOODIE LARGE",

    # Irrelevant North Face items
    "TNF BC DUFFEL BLK L",
    "NORTHFACE BC DAYPACK BLK",
]

query = "TNF BASE CAMP HOODIE BLK L"
top_matches = process.extract(query, northface_skus, limit=2)
print(top_matches)
```

Using fuzzy matching, we might get something like this:

```text
('TNF BC HDY BLK L', 86)
('NF BASE CAMP ZIP HOODIE LARGE', 76)
```

At first glance, this is not terrible. We managed to exclude the duffel and the daypack, which are clearly unrelated. But there is still a problem. The second result is a zip hoodie. That is not the same product. It just happens to share a lot of overlapping words with the query.

This is one of the big limitations of fuzzy matching. It rewards text overlap, but it does not really understand which words are **product defining** and which words are simply alternative representations of another word.

In this example, ZIP / FULLZIP is not just another token. It changes the product.

You can squeeze a little more performance out of this approach through normalization:

- Converting all item names to upper case before comparing
- Trimming leading and trailing spaces
- Removing punctuation that does not matter
- Replacing known terms like NF, TNF, and North Face with one canonical value
- Standardizing common values like BLK / Black or L / Large

These steps will help. In fact, they are probably worth doing no matter what. The reason is that a single item name might have three or four important terms, and each of those terms might have several valid ways of being written. Once that happens, you are dealing with a domain language problem. 

## A Hybrid Approach

At this point, you have probably googled around and stumbled into vector databases, embeddings, semantic search, or hybrid search.

If you are a Snowflake user, this is where Cortex Search becomes very useful. Cortex Search combines semantic similarity and lexical similarity. In other words, it can search based on meaning while still paying attention to the actual text. Even though this by itself is not the most complete solution ~ **in my opinion** ~ in many cases it will be more than satisfactory to stop here. However, we will build on top of this solution for our final architecture so it is still worth exploring.

If you are not using Snowflake, the same general idea exists in other systems. Pinecone, Qdrant, Weaviate, Elasticsearch, and other search/vector systems can all support some version of hybrid retrieval. The implementation details will be different, but the core idea is the same.

For this post, I am going to focus on Snowflake. The basic idea is that we normalize our item names, build a Cortex Search service on top of the normalized text, and then query that service whenever we want to find possible duplicate records.

```sql
CREATE OR REPLACE CORTEX SEARCH SERVICE DATABASE.SCHEMA.NORMALIZED_ITEM_SEARCH
    ON NORMALIZED_ITEM_NAME -- The indexed column
    ATTRIBUTES ITEM_KEY
    WAREHOUSE = YOUR_WAREHOUSE
    TARGET_LAG = '1 day' -- Adjust based on how often your table updates
    EMBEDDING_MODEL = 'snowflake-arctic-embed-l-v2.0'
AS (
    SELECT
        NORMALIZED_ITEM_NAME,
        ITEM_KEY,
        ITEM_NUMBER,
        ITEM_NAME
    FROM DATABASE.SCHEMA.ITEM_NORMALIZED
);
```

From here, we can create a stored procedure that takes an input item name, optionally filters the search space by something like product line, and returns the top matches from Cortex Search.

```sql
CREATE OR REPLACE PROCEDURE DATABASE.SCHEMA.FIND_DUPLICATES_V1(
    "INPUT_ITEM_NAME" VARCHAR,
    "PRODUCT_LINE" VARCHAR DEFAULT NULL,
    "TOP_N" NUMBER(38,0) DEFAULT 10
)
RETURNS TABLE (
    "RANK" NUMBER(38,0),
    "COSINE_SIMILARITY" FLOAT,
    "TEXT_MATCH" FLOAT,
    "RERANKER_SCORE" FLOAT,
    "ITEM_NUMBER" VARCHAR,
    "ITEM_NAME" VARCHAR
)
LANGUAGE SQL
EXECUTE AS OWNER
AS '
DECLARE
  res RESULTSET;
  query_str VARCHAR;
  json_request VARCHAR;
BEGIN
  query_str := REPLACE(
    REGEXP_REPLACE(TRIM(UPPER(:INPUT_ITEM_NAME)), ''[^A-Z0-9/\\\\.]+'', '' ''),
    ''"'',
    ''\\\\"''
  );

  json_request := ''{"query":"'' || :query_str || ''","columns":["ITEM_NUMBER","ITEM_NAME"],"scoring_config":{"weights":{"texts":2,"vectors":4,"reranker":3}}'';

  IF (:PRODUCT_LINE IS NOT NULL) THEN
    json_request := json_request || '',"filter":{"@eq":{"PRODUCT_LINE_DESC":"'' || REPLACE(:PRODUCT_LINE, ''"'', ''\\\\"'') || ''"}}'';
  END IF;

  json_request := json_request || '',"limit":'' || :TOP_N::VARCHAR || ''}'';

  res := (
    SELECT
      ROW_NUMBER() OVER (ORDER BY r.INDEX) AS RANK,
      ROUND(r.value:"@scores":"cosine_similarity"::FLOAT, 4) AS COSINE_SIMILARITY,
      ROUND(r.value:"@scores":"text_match"::FLOAT, 4) AS TEXT_MATCH,
      ROUND(r.value:"@scores":"reranker_score"::FLOAT, 4) AS RERANKER_SCORE,
      r.value:ITEM_NUMBER::VARCHAR AS ITEM_NUMBER,
      r.value:ITEM_NAME::VARCHAR AS ITEM_NAME
    FROM TABLE(FLATTEN(
      PARSE_JSON(
        SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
          ''DATABASE.SCHEMA.NORMALIZED_ITEM_SEARCH'',
          :json_request
        )
      ):results
    )) r
  );

  RETURN TABLE(res);
END;
';
```


This gives us three useful signals:

- `COSINE_SIMILARITY`: how close the records are semantically
- `TEXT_MATCH`: how much lexical overlap exists
- `RERANKER_SCORE`: Snowflake's second pass judgment over the retrieved candidates using some form of cross/bi encoder.


You can adjust the weight of each signal in the request to the search service like so:

```json
"scoring_config": {
  "weights": {
    "texts": 2,
    "vectors": 4,
    "reranker": 3
  }
}
```

In my case, cosine similarity was usually the strongest signal, followed by the reranker, followed by text match. Your data might behave differently, so this is worth testing.

This approach already works pretty well.

For many item names, Cortex Search gives strong recall because it can understand that things like Large / LG, Pound / LB, and Black / BLK are related. In a lot of practical cases, stopping here may be good enough.

But I still ran into one major and stubborn issue.

You might find cases that in item names without a lot of surrounding context, or names made up of primarily business specific terms and abbreviations the model will still struggle to find accurate information. 

## The Problem With Private Abbreviations

Embedding models are good, but they are not magic.

They can often infer common abbreviations, common product terms, and common semantic relationships. But they can struggle when the important words are business specific or too short to carry much context.

Take for example a real case from my problem:

```text
KBBB -> Knock Out Bond Beam
```

If this abbreviation is specific to your business, your catalog, or your manufacturing process, there is no guarantee that a general purpose embedding model will understand it. 

Imagine how painful this becomes when item names are short (Made up of 2/3 words were one is KOBB).

If an item name has a lot of surrounding context, the model might still recover. But if the name is mostly abbreviations, there may not be enough signal for the embedding model to work with

So what do we do?

One option would be to fine tune an embedding model on our own item data. That might work, but it comes with a lot of overhead: 

* You need training data
- You need a process for evaluating the model
- You need to keep the model updated as new terms appear
- You need infrastructure for training, deployment, and monitoring
- It is not something Snowflake gives you natively out of the box

That felt heavier than what I wanted and the alternative I landed on was simpler:

Use an LLM to generate alternate versions of the query using a controlled abbreviation and synonym map, then search each version through Cortex Search separately.

## A More Optimal Approach

The key detail is control. I am not asking the LLM to be creative. I am not asking it to guess missing words. And I am not asking it to invent better product names. 

Instead, I give it a mapping of known abbreviations and synonyms, then ask it to generate a small number of alternate query strings using only those mapped replacements.

For example, if the input is:

```text
TNF BASE CAMP HOODIE BLK L
```

And the synonym map contains:

```text
TNF / NF / North Face
BASE CAMP / BASECAMP / BC
HOODIE / HDY / PULLOVER / PO
BLK / BLACK
L / LARGE / LRG
```

Then acceptable variants would look like:

```text
NORTHFACE BC PULLOVER BLACK LARGE
NF BASECAMP HDY BLK L
TNF BC PO BLACK LRG
```

Notice what is not happening here. The LLM is not adding "zip". It is not removing the size. It is not changing the color. It is not deciding that a hoodie and a jacket are close enough. It is only expanding, contracting, and mixing known mapped terms.

That distinction is important because it keeps the system useful without letting it become unpredictable. Below is the architecture I came up with to solve this problem:

![Snowflake item deduplication flow](/assets/blog/entity-resolution/snowflake-item-dedup-flow.svg)

The flow is:

1. Normalize the input query.
2. Run Cortex Search on the original query.
3. Send the normalized query plus the abbreviation map to `AI_COMPLETE` / LLM of choice. Note: Pick something fast, cheap, but decently intelligent here.
4. Ask for a structured JSON response containing up to N alternate query variants. In our case we chose up to 3.
5. Run Cortex Search for each generated variant.
6. `UNION ALL` the original and variant results.
7. Deduplicate by `ITEM_NUMBER`, keeping the one with highest cosine similarity for items that appear more than once.
8. Rank each candidate independently by cosine similarity, text match, and reranker score.
9. Fuse those ranks using reciprocal rank fusion.
10. Sort by the final RRF score and return the top results.

There are two parts of this architecture that matter most.

First, we still search the original query. If the user already typed the best representation of the item, we do not want to lose that signal. The AI generated variants are there to improve recall, not replace the original query.

Second, we deduplicate before final ranking. The same item might appear from the original query and from multiple variants. That is actually a good sign, because it means several different representations are pointing to the same candidate. But we do not want to return the same item multiple times. So after combining all search results, we partition by `ITEM_NUMBER` and keep the strongest row for each item using the highest cosine similarity.

## Why Reciprocal Rank Fusion?

At this point, we have several signals:

- cosine similarity
- text match
- reranker score
- search source

One option would be to create a weighted average of the raw scores. But raw scores will not work here as different search results will live on different scales and some scores such as text and reranker are unbounded.

Instead, I used reciprocal rank fusion (RRF). The idea is simple: rank each candidate independently by each signal, then reward candidates that consistently appear near the top.

```sql
RRF = 1 / (60 + RANK_COS)
    + 1 / (60 + RANK_TXT)
    + 1 / (60 + RANK_RR)
```

This makes the final ranking less dependent on the exact raw score values and more dependent on whether the candidate performs well across multiple signals. In practice, this is useful because a strong duplicate candidate usually does not win on only one metric. It tends to show up well semantically, lexically, and through the reranker.

## Full Snowflake Procedure

Below is the full stored procedure version of the architecture. I am using generic `DATABASE.SCHEMA` placeholders here, and have erased the mapping dictionary for privacy but this is a close representation of the production procedure:

```sql
CREATE OR REPLACE PROCEDURE DATABASE.SCHEMA.FIND_DUPLICATES(
    "INPUT_ITEM_NAME" VARCHAR,
    "PRODUCT_LINE" VARCHAR DEFAULT NULL,
    "TOP_N" NUMBER(38,0) DEFAULT 20
)
RETURNS TABLE (
    "RANK" NUMBER(38,0),
    "RRF_SCORE" FLOAT,
    "COSINE_SIMILARITY" FLOAT,
    "TEXT_MATCH" FLOAT,
    "RERANKER_SCORE" FLOAT,
    "SEARCH_SOURCE" VARCHAR,
    "ITEM_NUMBER" VARCHAR,
    "ITEM_NAME" VARCHAR,
)
LANGUAGE SQL
EXECUTE AS OWNER
AS '
DECLARE
    res RESULTSET;
    query_str VARCHAR;
    filter_str VARCHAR;
    base_request VARCHAR;
    llm_response VARCHAR;
    variant_query VARCHAR;
    search_limit NUMBER;
BEGIN
    search_limit := GREATEST(:TOP_N, 10);

    query_str := REPLACE(REGEXP_REPLACE(TRIM(UPPER(:INPUT_ITEM_NAME)), ''[^A-Z0-9/\\\\.]+'', '' ''), ''"'', ''\\\\"'');

    filter_str := '''';
    IF (:PRODUCT_LINE IS NOT NULL) THEN
        filter_str := '',"filter":{"@eq":{"PRODUCT_LINE_DESC":"'' || REPLACE(:PRODUCT_LINE, ''"'', ''\\\\"'') || ''"}}'';
    END IF;

    base_request := ''{"query":"'' || :query_str || ''","columns":["ITEM_NUMBER","ITEM_NAME"],"scoring_config":{"weights":{"texts":3,"vectors":4,"reranker":3}}'' || :filter_str || '',"limit":'' || :search_limit::VARCHAR || ''}'';

    CREATE OR REPLACE TEMPORARY TABLE DATABASE.SCHEMA.TMP_SEARCH_RESULTS (
        SEARCH_SOURCE VARCHAR,
        COSINE_SIMILARITY FLOAT,
        TEXT_MATCH FLOAT,
        RERANKER_SCORE FLOAT,
        ITEM_NUMBER VARCHAR,
        ITEM_NAME VARCHAR,
    );

    INSERT INTO DATABASE.SCHEMA.TMP_SEARCH_RESULTS
    SELECT
        ''ORIGINAL'' AS SEARCH_SOURCE,
        r.value:"@scores":"cosine_similarity"::FLOAT,
        r.value:"@scores":"text_match"::FLOAT,
        r.value:"@scores":"reranker_score"::FLOAT,
        r.value:ITEM_NUMBER::VARCHAR,
        r.value:ITEM_NAME::VARCHAR,
    FROM TABLE(FLATTEN(
        PARSE_JSON(
            SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
                ''DATABASE.SCHEMA.NORMALIZED_ITEM_SEARCH'',
                :base_request
            )
        ):results
    )) r;

    llm_response := SNOWFLAKE.CORTEX.AI_COMPLETE(
        ''llama4-maverick'',
        ''You are a building materials product naming expert.

TASK: Generate up to 3 alternate versions of the item name by:

1. Replacing abbreviations with full forms, or full forms with abbreviations
2. Converting units ONLY when a unit label is explicitly attached to a number

ABBREVIATION DICTIONARY:

**EXCLUDED FOR PRIVACY**

DO NOT CHANGE:
- Dimensions (8x8x16, 12x8x16) — never split, modify, or add units
- Colors, product codes, symbols
- Never add units to a number that has none
- Never drop any part of the name

Item: "'' || :query_str || ''"'',
        response_format => TYPE OBJECT(alternates ARRAY(VARCHAR))
    );

    llm_response := TRIM(llm_response);

    IF (:llm_response IS NOT NULL AND LENGTH(:llm_response) > 2) THEN
        LET parsed_alternates VARIANT := TRY_PARSE_JSON(:llm_response);
        LET alt_count NUMBER := 0;

        IF (:parsed_alternates IS NOT NULL AND :parsed_alternates:alternates IS NOT NULL) THEN
            alt_count := COALESCE(ARRAY_SIZE(:parsed_alternates:alternates), 0);
        END IF;

        IF (:alt_count > 3) THEN
            alt_count := 3;
        END IF;

        LET i NUMBER := 0;
        WHILE (:i < :alt_count) DO
            variant_query := REPLACE(REGEXP_REPLACE(TRIM(UPPER(GET(:parsed_alternates:alternates, :i)::VARCHAR)), ''[^A-Z0-9/\\\\.]+'', '' ''), ''"'', ''\\\\"'');

            IF (LENGTH(:variant_query) > 2 AND :variant_query != :query_str) THEN
                LET variant_request VARCHAR := ''{"query":"'' || :variant_query || ''","columns":["ITEM_NUMBER","ITEM_NAME"],"scoring_config":{"weights":{"texts":3,"vectors":4,"reranker":3}}'' || :filter_str || '',"limit":'' || :search_limit::VARCHAR || ''}'';

                INSERT INTO DATABASE.SCHEMA.TMP_SEARCH_RESULTS
                SELECT
                    ''VARIANT_'' || (:i + 1)::VARCHAR AS SEARCH_SOURCE,
                    r.value:"@scores":"cosine_similarity"::FLOAT,
                    r.value:"@scores":"text_match"::FLOAT,
                    r.value:"@scores":"reranker_score"::FLOAT,
                    r.value:ITEM_NUMBER::VARCHAR,
                    r.value:ITEM_NAME::VARCHAR
                FROM TABLE(FLATTEN(
                    PARSE_JSON(
                        SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
                            ''DATABASE.SCHEMA.NORMALIZED_ITEM_SEARCH'',
                            :variant_request
                        )
                    ):results
                )) r;
            END IF;

            i := i + 1;
        END WHILE;
    END IF;

    res := (
        WITH deduped AS (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY ITEM_NUMBER
                    ORDER BY COSINE_SIMILARITY DESC NULLS LAST
                ) AS rn
            FROM DATABASE.SCHEMA.TMP_SEARCH_RESULTS
        ),
        ranked AS (
            SELECT
                COSINE_SIMILARITY,
                TEXT_MATCH,
                RERANKER_SCORE,
                SEARCH_SOURCE,
                ITEM_NUMBER,
                ITEM_NAME,
                RANK() OVER (ORDER BY COSINE_SIMILARITY DESC NULLS LAST) AS RANK_COS,
                RANK() OVER (ORDER BY TEXT_MATCH DESC NULLS LAST) AS RANK_TXT,
                RANK() OVER (ORDER BY RERANKER_SCORE DESC NULLS LAST) AS RANK_RR
            FROM deduped
            WHERE rn = 1
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY
                (1.0 / (60 + RANK_COS)) + (1.0 / (60 + RANK_TXT)) + (1.0 / (60 + RANK_RR)) DESC
            )::NUMBER(38,0) AS RANK,
            ROUND((1.0 / (60 + RANK_COS)) + (1.0 / (60 + RANK_TXT)) + (1.0 / (60 + RANK_RR)), 6)::FLOAT AS RRF_SCORE,
            ROUND(COSINE_SIMILARITY, 4)::FLOAT AS COSINE_SIMILARITY,
            ROUND(TEXT_MATCH, 4)::FLOAT AS TEXT_MATCH,
            ROUND(RERANKER_SCORE, 4)::FLOAT AS RERANKER_SCORE,
            SEARCH_SOURCE::VARCHAR AS SEARCH_SOURCE,
            ITEM_NUMBER::VARCHAR AS ITEM_NUMBER,
            ITEM_NAME::VARCHAR AS ITEM_NAME,
        FROM ranked
        ORDER BY RRF_SCORE DESC
        LIMIT :TOP_N
    );

    DROP TABLE IF EXISTS DATABASE.SCHEMA.TMP_SEARCH_RESULTS;
    RETURN TABLE(res);
END;
';
```

## Why This Works Better

The biggest improvement comes from giving the search system more ways to say the same thing.

Cortex Search is still doing the heavy lifting. It is still retrieving candidates using semantic and lexical signals. The LLM is not deciding which item is the duplicate.

The LLM is only helping with query expansion. That is the important design choice. By constraining the LLM to a known transformation map, we get the benefit of domain specific language without giving up too much control. The system can search for the same item under several valid names, but it does not get to invent new product attributes.

This is especially helpful for manufacturing data because the same item might be described differently depending on who created the record or even which naming convention was in place at the time.

## Where This Still Needs Care

This approach is still not perfect.

The quality of the transformation map matters a lot. If the map is wrong, the generated variants will be wrong. If the map is incomplete, the system may still miss important duplicates.

You also need to be careful with abbreviations that mean different things in different contexts.

For example, an abbreviation might mean one thing for one product line and something completely different for another. In those cases, product line filters and context specific mappings become important.

Finally, you still need human review somewhere in the process.

The goal of this pipeline is not to automatically merge records without oversight. The goal is to surface the best duplicate candidates so that the review process becomes faster, more accurate, and less dependent on someone remembering every abbreviation in the business.

## Final Thoughts

The main lesson for me was that entity resolution is often less about finding one perfect algorithm and more about stacking several imperfect signals in a controlled way.

Exact matching is too strict.

Fuzzy matching is helpful, but it can confuse similar looking products.

Hybrid search is much stronger, but it can still miss domain specifiec abbreviations.

LLM generated query variants help fill that gap, as long as the LLM is constrained to known abbreviation and synonym.

Once those candidate sets are combined, deduplicated, and reranked with RRF, you end up with a much more practical duplicate detection workflow achieving accuracy well in the the %90+.

And the nice part is that the final system is still explainable.

For every returned candidate, you can inspect which query found it, how it scored on cosine similarity, how it scored on text match, how the reranker judged it, and how those signals contributed to the final rank.
