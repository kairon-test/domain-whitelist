from urllib.parse import urljoin

from augmentation.knowledge_graph.document_parser import DocumentParser
from augmentation.knowledge_graph.training_data_generator import TrainingDataGenerator
from kairon.data_processor.constant import TRAINING_DATA_GENERATOR_STATUS
from kairon.data_processor.processor import TrainingDataGenerationProcessor
from kairon.utils import Utility

# file deepcode ignore W0703: Any Exception should be updated as status for Training Data processor
def parse_document_and_generate_training_data(bot: str, user: str, token: str):
    """
    Function to parse pdf or docx documents and retrieve intents, responses and training examples from it

    :param bot: bot id
    :param user: user id
    :param token: token for user authentication
    :return: None
    """
    kairon_url = None
    try:
        if Utility.environment.get('data_generation') and Utility.environment['data_generation'].get('event_url'):
            Utility.trigger_data_generation_event(bot, user, token)
        else:
            if Utility.environment.get('data_generation') and Utility.environment['data_generation'].get('kairon_url'):
                kairon_url = Utility.environment['data_generation'].get('kairon_url')

            if kairon_url:
                status = {"status": TRAINING_DATA_GENERATOR_STATUS.INPROGRESS.value}
                Utility.http_request('PUT', urljoin(kairon_url, "/api/bot/processing-status"), token, user, status)
            else:
                TrainingDataGenerationProcessor.set_status(
                    bot=bot,
                    user=user,
                    status=TRAINING_DATA_GENERATOR_STATUS.INPROGRESS.value
                )
            kg_info = TrainingDataGenerationProcessor.fetch_latest_workload(bot, user)
            doc_path = kg_info['document_path']
            doc_structure, sentences = DocumentParser.parse(doc_path)
            training_data = TrainingDataGenerator.generate_intent(doc_structure, sentences)
            if kairon_url:
                status = {
                    "status": TRAINING_DATA_GENERATOR_STATUS.COMPLETED.value,
                    "response": training_data
                }
                Utility.http_request('PUT', urljoin(kairon_url, "/api/bot/processing-status"), token, user, status)
            else:
                TrainingDataGenerationProcessor.set_status(
                    bot=bot,
                    user=user,
                    status=TRAINING_DATA_GENERATOR_STATUS.COMPLETED.value,
                    response=training_data
                )
    except Exception as e:
        if kairon_url:
            status = {
                "status": TRAINING_DATA_GENERATOR_STATUS.FAIL.value,
                "exception": str(e)
            }
            Utility.http_request('PUT', urljoin(kairon_url, "/api/bot/processing-status"), token, user, status)
        else:
            TrainingDataGenerationProcessor.set_status(bot=bot, user=user, status=TRAINING_DATA_GENERATOR_STATUS.FAIL.value,
                                                       exception=str(e))
