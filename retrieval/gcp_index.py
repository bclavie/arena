import logging
import math
import json
from google.cloud import aiplatform, storage

from ..models import model_name_as_path
from .index import load_passages

logger = logging.getLogger(__name__)


class VertexIndex:
    """
    A GCP Vertex AI Vector Search wrapper.
    """
    index: aiplatform.MatchingEngineIndex = None
    endpoint: aiplatform.MatchingEngineIndexEndpoint = None
    PROJECT_ID = "[your_project_id]"
    REGION = "us-central1"
    MACHINE_TYPE = "e2-standard-16"
    GCS_BUCKET_NAME = "GCS_BUCKET_NAME"
    GCS_BUCKET_URI = f"gs://{GCS_BUCKET_NAME}"
    TMP_FILE_PATH = "tmp.json"

    def __init__(self, dim: int, model_name: str, model):
        aiplatform.init(project=self.PROJECT_ID, location=self.REGION)
        self.dim = dim
        self.model = model
        model_path = model_name_as_path(model_name)
        self.index_name = f"index_{model_path}"
        self.endpoint_name = f"endpoint_{model_path}"
        self.doc_map = dict()
    
    def _index_exists(self) -> bool:
        index_names = [
            index.resource_name
            for index in aiplatform.MatchingEngineIndex.list(
                filter=f"display_name={self.index_name}"
            )
        ]
        return len(index_names)

    def _write_embeddings_to_tmp_file(self, embeddings, indices):
        with open(self.TMP_FILE_PATH, "a") as f:
            embeddings_formatted = [
                json.dumps(
                    {
                        "id": str(index),
                        "embedding": [str(value) for value in embedding],
                    }
                )
                + "\n"
                for index, embedding in zip(indices, embeddings)
            ]
            f.writelines(embeddings_formatted)

    def _write_embeddings(self, gpu_embedder_batch_size=512) -> None:
        """Batch encoding passages, the write a jsonl file."""
        passages = load_passages(filenames=["corpus.jsonl"])
        self.doc_map = {i: doc for i, doc in enumerate(passages)}

        n_batch = math.ceil(len(passages) / gpu_embedder_batch_size)
        total = 0
        for i in range(n_batch):
            indices = range(i * gpu_embedder_batch_size, (i + 1) * gpu_embedder_batch_size)
            batch = passages[i * gpu_embedder_batch_size : (i + 1) * gpu_embedder_batch_size]
            if hasattr(self.model, "encode_corpus"):
                embeddings = self.model.encode_corpus(batch, batch_size=gpu_embedder_batch_size, convert_to_tensor=True)
            else:
                embeddings = self.model.encode(batch, batch_size=gpu_embedder_batch_size, convert_to_tensor=True)
            total += len(embeddings)
            self._write_embeddings_to_tmp_file(embeddings, indices)
            if i % 500 == 0 and i > 0:
                logger.info(f"Number of passages encoded: {total}")
        
        logger.info(f"{total} passages encoded.")

    def _upload_embedding_file(self)-> None:
        """Upload temp file to GCP storage bucket."""
        logger.info(f"Uploading {self.TMP_FILE_PATH} to {self.GCS_BUCKET_URI}")
        storage_client = storage.Client()
        bucket = storage_client.bucket(self.GCS_BUCKET_NAME)
        blob = bucket.blob(self.TMP_FILE_PATH)
        blob.upload_from_filename(self.TMP_FILE_PATH)


    def _create_index(self) -> None:
        """Create empty index and update it with embeddings."""
        logger.info(f"Creating Vector Search index {self.index_name} ...")
        self.index = aiplatform.MatchingEngineIndex.create_tree_ah_index(
            display_name=self.index_name,
            dimensions=self.dim,
            distance_measure_type="DOT_PRODUCT_DISTANCE",
            shard_size="SHARD_SIZE_SMALL",
            index_update_method="STREAM_UPDATE",  # allowed values BATCH_UPDATE , STREAM_UPDATE
        )
        logger.info(
            f"Vector Search index {self.index.display_name} created with resource name {self.index.resource_name}"
        )

        self._write_embeddings()
        self._upload_embedding_file()

        self.index.update_embeddings(
            contents_delta_uri=self.GCS_BUCKET_URI,
        )
    

    def load_index(self) -> None:
        """Load self.index if exists. Create and load index if not."""
        if self._index_exists():
            self.index = aiplatform.MatchingEngineIndex(index_name=self.index_name)
            logger.info(f"Vector Search index {self.index.display_name} exists with resource name {self.index.resource_name}")
            return
        self._create_index()
    
    def _endpoint_exists(self) -> bool:
        endpoint_names = [
            endpoint.resource_name
            for endpoint in aiplatform.MatchingEngineIndexEndpoint.list(
                filter=f"display_name={self.endpoint_name}"
            )
        ]
        return len(endpoint_names)

    def load_endpoint(self) -> None:
        """Load a public endpoint if exists. Create and load endpoint if not."""
        if self.index is None:
            self.load_index()

        if self._endpoint_exists():
            self.endpoint = aiplatform.MatchingEngineIndexEndpoint(
                index_endpoint_name=self.endpoint_name
            )
            logger.info(
                f"Vector Search index endpoint {self.endpoint.display_name} exists with resource name {self.endpoint.resource_name}"
            )
            return 
        
        logger.info(
            f"Deploying Vector Search index {self.index.display_name} at endpoint {self.endpoint.display_name} ..."
        )
        self.endpoint = self.endpoint.deploy_index(
            index=self.index,
            deployed_index_id=self.index_name,
            display_name=self.index_name,
            machine_type=self.MACHINE_TYPE,
            min_replica_count=1,
            max_replica_count=1,
        )
        logger.info(
            f"Vector Search index {self.index.display_name} is deployed at endpoint {self.endpoint.display_name}"
        )
    
    def search(self, query_embeds: list, topk=1):
        """Return topk docs and scores."""
        if self.endpoint is None:
            self.load_endpoint()

        response = self.endpoint.match(
            deployed_index_id=self.index_name,
            queries=query_embeds,
            num_neighbors=topk,
        )
        sorted_data = sorted(response[0], key=lambda x: x.distance, reverse=True)
        docs = [self.doc[x.id] for x in sorted_data]
        return docs
