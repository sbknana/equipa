============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.0.2, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /srv/forge-share/AI_Stuff/Equipa-repo
plugins: asyncio-1.3.0, anyio-4.12.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 20 items

tests/test_vector_memory.py::TestCosineSimilarity::test_identical_vectors PASSED [  5%]
tests/test_vector_memory.py::TestCosineSimilarity::test_orthogonal_vectors PASSED [ 10%]
tests/test_vector_memory.py::TestCosineSimilarity::test_opposite_vectors PASSED [ 15%]
tests/test_vector_memory.py::TestCosineSimilarity::test_unit_vectors PASSED [ 20%]
tests/test_vector_memory.py::TestCosineSimilarity::test_zero_length_vector PASSED [ 25%]
tests/test_vector_memory.py::TestCosineSimilarity::test_mismatched_dimensions PASSED [ 30%]
tests/test_vector_memory.py::TestCosineSimilarity::test_empty_vectors PASSED [ 35%]
tests/test_vector_memory.py::TestGetRelevantEpisodesVectorMemoryOff::test_keyword_scoring_without_vector_memory PASSED [ 40%]
tests/test_vector_memory.py::TestGetRelevantEpisodesVectorMemoryOn::test_vector_memory_on_does_not_crash PASSED [ 45%]
tests/test_vector_memory.py::TestRecordAgentEpisodeEmbedding::test_record_episode_with_vector_memory_on PASSED [ 50%]
tests/test_vector_memory.py::TestRecordAgentEpisodeEmbedding::test_embedding_not_called_with_vector_memory_off PASSED [ 55%]
tests/test_vector_memory.py::TestRecordAgentEpisodeEmbedding::test_embedding_failure_does_not_block_recording PASSED [ 60%]
tests/test_vector_memory.py::TestEndToEndVectorMemory::test_insert_and_retrieve_workflow PASSED [ 65%]
tests/test_vector_memory.py::TestEndToEndVectorMemory::test_dissimilar_query_ranks_lower PASSED [ 70%]
tests/test_vector_memory.py::TestOllamaMocking::test_get_embedding_mocks_urllib PASSED [ 75%]
tests/test_vector_memory.py::TestOllamaMocking::test_get_embedding_handles_timeout PASSED [ 80%]
tests/test_vector_memory.py::TestOllamaMocking::test_get_embedding_handles_connection_error PASSED [ 85%]
tests/test_vector_memory.py::TestFindSimilarByEmbedding::test_find_similar_returns_sorted_by_similarity PASSED [ 90%]
tests/test_vector_memory.py::TestFindSimilarByEmbedding::test_find_similar_returns_empty_on_ollama_failure PASSED [ 95%]
tests/test_vector_memory.py::TestFindSimilarByEmbedding::test_find_similar_invalid_table_returns_empty PASSED [100%]

============================== 20 passed in 1.26s ==============================
