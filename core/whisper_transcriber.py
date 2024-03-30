import logging
import os
import tempfile

import torch
import whisper
from minio import Minio
from opentelemetry import trace
from opentelemetry.trace import StatusCode


class WhisperTranscriber:
    def __init__(self, model_name: str, minio_endpoint: str, minio_access_key: str, minio_secret_key: str, minio_bucket: str, minio_use_ssl: bool | str):
        self.tracer = trace.get_tracer(__name__)
        with self.tracer.start_as_current_span("Initialize WhisperTranscriber"):
            self.model_name = model_name
            self.model = self.model_load()
            self.minio_client = Minio(
                minio_endpoint,
                access_key=minio_access_key,
                secret_key=minio_secret_key,
                secure=minio_use_ssl,
            )
            self.bucket = minio_bucket

    def model_load(self):
        with self.tracer.start_as_current_span("Load Whisper core"):
            logging.info(f"Load Whisper core, {self.model_name}")
            return whisper.load_model(self.model_name)

    def transcribe_audio(self, object_name: str) -> tuple[str, str]:
        is_cuda_available = torch.cuda.is_available()
        logging.debug(f"Is CUDA available: {is_cuda_available}")
        with self.tracer.start_as_current_span("Transcribe audio"):
            temp_file_path = self._get_file_from_minio(object_name)
            if temp_file_path is None:
                return "error", "Could not retrieve file"
            try:
                logging.info("Start transcription")
                result = self.model.transcribe(temp_file_path, fp16=True if is_cuda_available else False)
            finally:
                os.remove(temp_file_path)
                logging.info("Removed temp file")
                if is_cuda_available:
                    torch.cuda.empty_cache()
            return result["language"], result["text"]

    def _get_file_from_minio(self, object_name: str) -> str | None:
        with self.tracer.start_as_current_span("Get file from minIO"):
            logging.info("Get object from S3")
            try:
                object_data = self.minio_client.get_object(self.bucket, object_name)
                logging.debug("Read object data into memory")
                object_bytes = object_data.data
            except Exception as e:
                current_tracer = trace.get_current_span()
                current_tracer.record_exception(e)
                current_tracer.set_status(status=StatusCode.ERROR, description=str(e))
                logging.error(f"Could not get object from S3. Error was: {e}")
                return
            else:
                logging.debug("Successfully read object data into memory")
                object_data.close()
            finally:
                object_data.release_conn()

            logging.debug("Create a temporary file and write the object bytes")
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                current_tracer = trace.get_current_span()
                current_tracer.add_event(name="Save temp file", attributes={"temp_file": temp_file.name})
                temp_file.write(object_bytes)
                temp_file_path = temp_file.name
                return temp_file_path


if __name__ == "__main__":
    from pathlib import Path

    from dotenv import load_dotenv

    dotenv_path = Path("/var/whisper/.env.local")
    load_dotenv(dotenv_path=dotenv_path)

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

    transcriber = WhisperTranscriber(
        model_name="large",
        minio_endpoint=os.getenv("MINIO_ENDPOINT"),
        minio_access_key=os.getenv("MINIO_ACCESS_KEY"),
        minio_secret_key=os.getenv("MINIO_SECRET_KEY"),
        minio_bucket=os.getenv("MINIO_BUCKET"),
        minio_use_ssl=bool(int(os.getenv("MINIO_USE_SSL"))),
    )

    language, text = transcriber.transcribe_audio("00239eb5-e493-11ee-af50-0242ac120006")
    logging.info(f"Detected language: {language}")
    logging.info(f"Recognized text: {text}")