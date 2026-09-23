import io
import json
import os
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np


class FaceEngine:
    """
    Biometric Face Verification Engine using OpenCV YuNet (Detector) and SFace (Feature Extractor).
    Extracts 128-D normalized metric embeddings for 1-shot biometric enrollment and anti-proxy verification.
    """

    def __init__(
        self,
        yunet_model_path: str = "models/face_detection_yunet.onnx",
        sface_model_path: str = "models/face_recognition_sface.onnx",
        score_threshold: float = 0.60,
        nms_threshold: float = 0.30,
        match_threshold: float = 0.55,
    ):
        self.yunet_path = Path(yunet_model_path)
        self.sface_path = Path(sface_model_path)
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.match_threshold = match_threshold

        if not self.yunet_path.exists():
            raise FileNotFoundError(f"YuNet model not found at {self.yunet_path}")
        if not self.sface_path.exists():
            raise FileNotFoundError(f"SFace model not found at {self.sface_path}")

        # Initialize detector with default dummy size; dynamic resizing occurs per input image
        self.detector = cv2.FaceDetectorYN.create(
            model=str(self.yunet_path),
            config="",
            input_size=(320, 320),
            score_threshold=self.score_threshold,
            nms_threshold=self.nms_threshold,
            top_k=5000,
        )

        self.recognizer = cv2.FaceRecognizerSF.create(
            model=str(self.sface_path),
            config="",
        )

    def extract_face_and_embedding(
        self, bgr_image: np.ndarray
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """
        Detects primary face, aligns and crops it, and extracts the 128-D feature embedding.
        Returns:
            Tuple of (aligned_face_bgr, embedding_128d) or None if no face is detected.
        """
        if bgr_image is None or bgr_image.size == 0:
            return None

        h, w = bgr_image.shape[:2]
        if h < 20 or w < 20:
            return None

        self.detector.setInputSize((w, h))
        retval, faces = self.detector.detect(bgr_image)

        if faces is None or len(faces) == 0:
            return None

        # Choose the highest confidence face (sorted descending by confidence at index 14)
        best_face = sorted(faces, key=lambda f: float(f[14]), reverse=True)[0]
        if float(best_face[14]) < self.score_threshold:
            return None

        aligned_face = self.recognizer.alignCrop(bgr_image, best_face)
        feature = self.recognizer.feature(aligned_face)

        return aligned_face, feature.flatten().astype(np.float32)

    def extract_embedding_from_bytes(
        self, image_bytes: bytes
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """
        Decodes raw image bytes (e.g. uploaded JPEG/PNG or base64) and returns (aligned_face, embedding).
        """
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        return self.extract_face_and_embedding(img)

    def compute_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        """
        Computes Cosine similarity between two feature vectors.
        Returns value typically in range [-1.0, 1.0], where > 0.55 indicates matching identity.
        """
        e1 = emb1.reshape(1, -1).astype(np.float32)
        e2 = emb2.reshape(1, -1).astype(np.float32)

        # OpenCV SFace matcher using cosine similarity
        sim = self.recognizer.match(e1, e2, cv2.FaceRecognizerSF_FR_COSINE)
        return float(sim)

    def verify_match(
        self, emb1: np.ndarray, emb2: np.ndarray, threshold: Optional[float] = None
    ) -> Tuple[bool, float]:
        """
        Verifies if two embeddings belong to the same identity.
        """
        t = threshold if threshold is not None else self.match_threshold
        sim = self.compute_similarity(emb1, emb2)
        return sim >= t, sim

    @staticmethod
    def embedding_to_json(embedding: np.ndarray) -> str:
        """Encodes embedding as JSON string for simple, portable database storage."""
        return json.dumps(embedding.tolist())

    @staticmethod
    def json_to_embedding(json_str: str) -> Optional[np.ndarray]:
        """Decodes JSON string to float32 numpy embedding."""
        if not json_str:
            return None
        try:
            arr = json.loads(json_str)
            return np.array(arr, dtype=np.float32)
        except Exception:
            return None
