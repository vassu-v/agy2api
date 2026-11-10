"""
Base Router - Shared routing base functionality
Provides model list processing, common responses, and shared features
"""

from datetime import datetime, timezone
from typing import List

from src.models import Model, ModelList

def create_openai_model_list(
    model_ids: List[str],
    owned_by: str = "google"
) -> ModelList:
    """
    Create an OpenAI-formatted model list
    
    Args:
        model_ids: List of model IDs
        owned_by: Model owner
        
    Returns:
        ModelList object
    """
    current_timestamp = int(datetime.now(timezone.utc).timestamp())
    
    models = [
        Model(
            id=model_id,
            object='model',
            created=current_timestamp,
            owned_by=owned_by
        )
        for model_id in model_ids
    ]
    
    return ModelList(data=models)


def create_gemini_model_list(
    model_ids: List[str],
    base_name_extractor=None
) -> dict:
    """
    Create a Gemini-formatted model list
    
    Args:
        model_ids: List of model IDs
        base_name_extractor: Optional base model name extractor function
        
    Returns:
        Dictionary containing the model list
    """
    gemini_models = []
    
    for model_id in model_ids:
        base_model = model_id
        if base_name_extractor:
            try:
                base_model = base_name_extractor(model_id)
            except Exception:
                pass
        
        model_info = {
            "name": f"models/{model_id}",
            "baseModelId": base_model,
            "version": "001",
            "displayName": model_id,
            "description": f"Gemini {base_model} model",
            "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
        }
        gemini_models.append(model_info)
    
    return {"models": gemini_models}
