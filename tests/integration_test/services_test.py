import os
import re
import shutil
import tarfile
import tempfile
from io import BytesIO
from zipfile import ZipFile

import pytest
import responses
from fastapi.testclient import TestClient
from mongoengine import connect
from mongoengine.queryset.base import BaseQuerySet
from rasa.shared.utils.io import read_config_file

from kairon.api.app.main import app
from kairon.exceptions import AppException
from kairon.shared.importer.data_objects import ValidationLogs
from kairon.shared.account.processor import AccountProcessor
from kairon.shared.actions.data_objects import ActionServerLogs
from kairon.shared.auth import Authentication
from kairon.shared.data.constant import UTTERANCE_TYPE, EVENT_STATUS
from kairon.shared.data.data_objects import Stories, Intents, TrainingExamples, Responses, ChatClientConfig
from kairon.shared.data.model_processor import ModelProcessor
from kairon.shared.data.processor import MongoProcessor
from kairon.shared.data.training_data_generation_processor import TrainingDataGenerationProcessor
from kairon.shared.data.utils import DataUtility
from kairon.shared.models import StoryEventType
from kairon.shared.models import User
from kairon.shared.utils import Utility
import json


os.environ["system_file"] = "./tests/testing_data/system.yaml"
client = TestClient(app)
access_token = None
token_type = None


@pytest.fixture(autouse=True)
def setup():
    os.environ["system_file"] = "./tests/testing_data/system.yaml"
    Utility.load_environment()
    connect(**Utility.mongoengine_connection(Utility.environment['database']["url"]))
    AccountProcessor.load_system_properties()


def pytest_configure():
    return {'token_type': None,
            'access_token': None,
            'username': None,
            'bot': None
            }


def test_api_wrong_login():
    response = client.post(
        "/api/auth/login", data={"username": "test@demo.ai", "password": "Welcome@1"}
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert not actual["success"]
    assert actual["message"] == "User does not exist!"


def test_account_registration_error():
    response = client.post(
        "/api/account/registration",
        json={
            "email": "integration@demo.ai",
            "first_name": "Demo",
            "last_name": "User",
            "password": "welcome@1",
            "confirm_password": "welcome@1",
            "account": "integration",
            "bot": "integration",
        },
    )
    actual = response.json()
    assert actual["message"] == [
        {'loc': ['body', 'password'], 'msg': 'Missing 1 uppercase letter', 'type': 'value_error'}]
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["data"] is None


def test_account_registration():
    response = client.post(
        "/api/account/registration",
        json={
            "email": "integration@demo.ai",
            "first_name": "Demo",
            "last_name": "User",
            "password": "Welcome@1",
            "confirm_password": "Welcome@1",
            "account": "integration",
            "bot": "integration",
        },
    )
    actual = response.json()
    assert actual["message"] == "Account Registered!"
    response = client.post(
        "/api/account/registration",
        json={
            "email": "integration2@demo.ai",
            "first_name": "Demo",
            "last_name": "User",
            "password": "Welcome@1",
            "confirm_password": "Welcome@1",
            "account": "integration2",
            "bot": "integration2",
        },
    )
    actual = response.json()
    assert actual["message"] == "Account Registered!"


def test_api_wrong_password():
    response = client.post(
        "/api/auth/login", data={"username": "integration@demo.ai", "password": "welcome@1"}
    )
    actual = response.json()
    assert actual["error_code"] == 401
    assert not actual["success"]
    assert actual["message"] == "Incorrect username or password"


def test_api_login():
    email = "integration@demo.ai"
    response = client.post(
        "/api/auth/login",
        data={"username": email, "password": "Welcome@1"},
    )
    actual = response.json()
    assert all(
        [
            True if actual["data"][key] else False
            for key in ["access_token", "token_type"]
        ]
    )
    assert actual["success"]
    assert actual["error_code"] == 0
    pytest.access_token = actual["data"]["access_token"]
    pytest.token_type = actual["data"]["token_type"]
    pytest.username = email
    response = client.get(
        "/api/user/details",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    assert response['data']['user']['_id']
    assert response['data']['user']['email'] == 'integration@demo.ai'
    assert response['data']['user']['role'] == 'admin'
    assert response['data']['user']['bot']
    assert response['data']['user']['timestamp']
    assert response['data']['user']['status']
    assert response['data']['user']['bot_name']
    assert response['data']['user']['account_name'] == 'integration'
    assert response['data']['user']['first_name'] == 'Demo'
    assert response['data']['user']['last_name'] == 'User'


def test_add_bot():
    response = client.post(
        "/api/account/bot",
        json={"data": "covid-bot"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    assert response['message'] == 'Bot created'
    assert response['error_code'] == 0
    assert response['success']


def test_list_bots():
    response = client.get(
        "/api/account/bot",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    assert len(response['data']) == 2
    pytest.bot = response['data'][0]['_id']
    assert response['data'][0]['name'] == 'Hi-Hello'
    assert response['data'][1]['name'] == 'covid-bot'


def test_list_entities_empty():
    response = client.get(
        f"/api/bot/{pytest.bot}/entities",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token}
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert len(actual['data']) == 1
    assert actual["success"]


def test_no_default_intents_in_bot():
    response = client.get(
        f"/api/bot/{pytest.bot}/intents",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    intents = set([d['name'] for d in actual["data"]])
    assert 'nlu_fallback' not in intents
    assert 'restart' not in intents
    assert 'back' not in intents
    assert 'session_start' not in intents
    assert 'out_of_scope' not in intents
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_update_bot_name():
    response = client.put(
        f"/api/account/bot/{pytest.bot}",
        json={"data": "Hi-Hello-bot"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    assert response['message'] == 'Bot name updated'
    assert response['error_code'] == 0
    assert response['success']

    response = client.get(
        "/api/account/bot",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    assert len(response['data']) == 2
    pytest.bot = response['data'][0]['_id']
    assert response['data'][0]['name'] == 'Hi-Hello-bot'
    assert response['data'][1]['name'] == 'covid-bot'


@pytest.fixture()
def resource_test_upload_zip():
    data_path = 'tests/testing_data/yml_training_files'
    tmp_dir = tempfile.gettempdir()
    zip_file = os.path.join(tmp_dir, 'test')
    shutil.make_archive(zip_file, 'zip', data_path)
    pytest.zip = open(zip_file + '.zip', 'rb').read()
    yield "resource_test_upload_zip"
    os.remove(zip_file + '.zip')
    shutil.rmtree(os.path.join('training_data', pytest.bot))


def test_upload_zip(resource_test_upload_zip):
    files = (('training_files', ("data.zip", pytest.zip)),
             ('training_files', ("domain.yml", open("tests/testing_data/all/domain.yml", "rb"))))
    response = client.post(
        f"/api/bot/{pytest.bot}/upload?import_data=true&overwrite=false",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=files,
    )
    actual = response.json()
    assert actual["message"] == "Upload in progress! Check logs."
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]


def test_upload():
    files = (('training_files', ("nlu.md", open("tests/testing_data/all/data/nlu.md", "rb"))),
             ('training_files', ("domain.yml", open("tests/testing_data/all/domain.yml", "rb"))),
             ('training_files', ("stories.md", open("tests/testing_data/all/data/stories.md", "rb"))),
             ('training_files', ("config.yml", open("tests/testing_data/all/config.yml", "rb"))))
    response = client.post(
        f"/api/bot/{pytest.bot}/upload?import_data=true&overwrite=true",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=files,
    )
    actual = response.json()
    assert actual["message"] == "Upload in progress! Check logs."
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]


def test_upload_yml():
    files = (('training_files', ("nlu.yml", open("tests/testing_data/valid_yml/data/nlu.yml", "rb"))),
             ('training_files', ("domain.yml", open("tests/testing_data/valid_yml/domain.yml", "rb"))),
             ('training_files', ("stories.yml", open("tests/testing_data/valid_yml/data/stories.yml", "rb"))),
             ('training_files', ("config.yml", open("tests/testing_data/valid_yml/config.yml", "rb"))),
             (
                 'training_files', ("http_action.yml", open("tests/testing_data/valid_yml/http_action.yml", "rb")))
             )
    response = client.post(
        f"/api/bot/{pytest.bot}/upload",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=files,
    )
    actual = response.json()
    assert actual["message"] == "Upload in progress! Check logs."
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]


def test_list_entities():
    response = client.get(
        f"/api/bot/{pytest.bot}/entities",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token}
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert {e['name'] for e in actual["data"]} == {'bot', 'file', 'category', 'file_text', 'ticketid', 'file_error', 'priority', 'requested_slot', 'fdresponse'}
    assert actual["success"]


def test_train(monkeypatch):
    def mongo_store(*arge, **kwargs):
        return None

    def _mock_training_limit(*arge, **kwargs):
        return False

    monkeypatch.setattr(Utility, "get_local_mongo_store", mongo_store)
    monkeypatch.setattr(ModelProcessor, "is_daily_training_limit_exceeded", _mock_training_limit)

    response = client.post(
        f"/api/bot/{pytest.bot}/train",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Model training started."


def test_upload_limit_exceeded(monkeypatch):
    monkeypatch.setitem(Utility.environment['model']['data_importer'], 'limit_per_day', 2)
    response = client.post(
        f"/api/bot/{pytest.bot}/upload?import_data=true&overwrite=false",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files={'training_files': ("nlu.yml", open("tests/testing_data/yml_training_files/data/nlu.yml", "rb"))}
    )
    actual = response.json()
    assert actual["message"] == 'Daily limit exceeded.'
    assert actual["error_code"] == 422
    assert actual["data"] is None
    assert not actual["success"]


@responses.activate
def test_upload_using_event_overwrite(monkeypatch):
    token = Authentication.create_access_token(data={'sub': pytest.username})
    responses.add(
        responses.POST,
        "http://localhost/upload",
        status=200,
        match=[
            responses.json_params_matcher(
                [{'name': 'BOT', 'value': pytest.bot}, {'name': 'USER', 'value': pytest.username},
                 {'name': 'IMPORT_DATA', 'value': '--import-data'}, {'name': 'OVERWRITE', 'value': '--overwrite'}])],
    )

    monkeypatch.setitem(Utility.environment['model']['data_importer'], 'event_url', "http://localhost/upload")

    def get_token(*args, **kwargs):
        return token

    monkeypatch.setattr(Authentication, "create_access_token", get_token)
    monkeypatch.setitem(Utility.environment['model']['data_importer'], "event_url", "http://localhost/upload")
    response = client.post(
        f"/api/bot/{pytest.bot}/upload?import_data=true&overwrite=true",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=(('training_files', ("nlu.yml", open("tests/testing_data/yml_training_files/data/nlu.yml", "rb"))),
               ('training_files', ("domain.yml", open("tests/testing_data/yml_training_files/domain.yml", "rb"))),
               (
                   'training_files',
                   ("stories.yml", open("tests/testing_data/yml_training_files/data/stories.yml", "rb"))),
               ('training_files', ("config.yml", open("tests/testing_data/yml_training_files/config.yml", "rb"))),
               (
                   'training_files',
                   ("http_action.yml", open("tests/testing_data/yml_training_files/http_action.yml", "rb")))
               )
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Upload in progress! Check logs."

    # update status
    log = ValidationLogs.objects(event_status=EVENT_STATUS.TASKSPAWNED.value).get()
    log.event_status = EVENT_STATUS.COMPLETED.value
    log.save()


@responses.activate
def test_upload_using_event_append(monkeypatch):
    token = Authentication.create_access_token(data={'sub': pytest.username})
    responses.add(
        responses.POST,
        "http://localhost/upload",
        status=200,
        match=[
            responses.json_params_matcher(
                [{'name': 'BOT', 'value': pytest.bot}, {'name': 'USER', 'value': pytest.username},
                 {'name': 'IMPORT_DATA', 'value': '--import-data'}, {'name': 'OVERWRITE', 'value': ''}])],
    )

    monkeypatch.setitem(Utility.environment['model']['data_importer'], 'event_url', "http://localhost/upload")

    def get_token(*args, **kwargs):
        return token

    monkeypatch.setattr(Authentication, "create_access_token", get_token)
    monkeypatch.setitem(Utility.environment['model']['data_importer'], "event_url", "http://localhost/upload")
    response = client.post(
        f"/api/bot/{pytest.bot}/upload?import_data=true&overwrite=false",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=(('training_files', ("nlu.yml", open("tests/testing_data/yml_training_files/data/nlu.yml", "rb"))),
               ('training_files', ("domain.yml", open("tests/testing_data/yml_training_files/domain.yml", "rb"))),
               (
                   'training_files',
                   ("stories.yml", open("tests/testing_data/yml_training_files/data/stories.yml", "rb"))),
               ('training_files', ("config.yml", open("tests/testing_data/yml_training_files/config.yml", "rb"))),
               (
                   'training_files',
                   ("http_action.yml", open("tests/testing_data/yml_training_files/http_action.yml", "rb")))
               )
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Upload in progress! Check logs."


def test_model_testing_limit_exceeded(monkeypatch):
    monkeypatch.setitem(Utility.environment['model']['test'], 'limit_per_day', 0)
    response = client.post(
        url=f"/api/bot/{pytest.bot}/test",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert actual['message'] == 'Daily limit exceeded.'
    assert not actual["success"]


@responses.activate
def test_model_testing_event(monkeypatch):
    event_url = 'http://event.url'
    monkeypatch.setitem(Utility.environment['model']['test'], 'event_url', event_url)
    responses.add("POST",
                  event_url,
                  json={"message": "Event triggered successfully!"},
                  status=200)
    response = client.post(
        url=f"/api/bot/{pytest.bot}/test",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual['message'] == 'Testing in progress! Check logs.'
    assert actual["success"]


def test_model_testing_in_progress():
    response = client.post(
        url=f"/api/bot/{pytest.bot}/test",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert actual['message'] == 'Event already in progress! Check logs.'
    assert not actual["success"]


def test_get_model_testing_logs():
    response = client.get(
        url=f"/api/bot/{pytest.bot}/logs/test",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual['data']
    assert actual["success"]


def test_get_data_importer_logs():
    response = client.get(
        f"/api/bot/{pytest.bot}/importer/logs",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual["data"]) == 5
    assert actual['data'][0]['event_status'] == EVENT_STATUS.TASKSPAWNED.value
    assert set(actual['data'][0]['files_received']) == {'stories', 'nlu', 'domain', 'config', 'http_actions'}
    assert actual['data'][0]['is_data_uploaded']
    assert actual['data'][0]['start_timestamp']
    assert actual['data'][2]['start_timestamp']
    assert actual['data'][2]['end_timestamp']
    assert set(actual['data'][2]['files_received']) == {'stories', 'nlu', 'domain', 'config', 'http_actions'}
    del actual['data'][2]['start_timestamp']
    del actual['data'][2]['end_timestamp']
    del actual['data'][2]['files_received']
    assert actual['data'][2] == {'intents': {'count': 14, 'data': []}, 'utterances': {'count': 14, 'data': []}, 'stories': {'count': 16, 'data': []}, 'training_examples': {'count': 192, 'data': []}, 'domain': {'intents_count': 19, 'actions_count': 27, 'slots_count': 10, 'utterances_count': 14, 'forms_count': 2, 'entities_count': 8, 'data': []}, 'config': {'count': 0, 'data': []}, 'rules': {'count': 1, 'data': []}, 'http_actions': {'count': 5, 'data': []}, 'exception': '', 'is_data_uploaded': True, 'status': 'Success', 'event_status': 'Completed'}
    assert actual['data'][3]['intents']['count'] == 16
    assert actual['data'][3]['intents']['data']
    assert actual['data'][3]['utterances']['count'] == 25
    assert actual['data'][3]['stories']['count'] == 16
    assert actual['data'][3]['stories']['data']
    assert actual['data'][3]['training_examples'] == {'count': 292, 'data': []}
    assert actual['data'][3]['domain'] == {'intents_count': 29, 'actions_count': 38, 'slots_count': 9, 'utterances_count': 25, 'forms_count': 2, 'entities_count': 8, 'data': []}
    assert actual['data'][3]['config'] == {'count': 0, 'data': []}
    assert actual['data'][3]['http_actions'] == {'count': 0, 'data': []}
    assert actual['data'][3]['is_data_uploaded']
    assert set(actual['data'][3]['files_received']) == {'stories', 'domain', 'config', 'nlu'}
    assert actual['data'][3]['status'] == 'Failure'
    assert actual['data'][3]['event_status'] == 'Completed'
    assert actual['data'][4]['rules']['count'] == 3
    assert not actual["message"]

    # update status for upload event
    log = ValidationLogs.objects(event_status=EVENT_STATUS.TASKSPAWNED.value).get()
    log.event_status = EVENT_STATUS.COMPLETED.value
    log.save()


def test_get_slots():
    response = client.get(
        f"/api/bot/{pytest.bot}/slots",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 8
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_add_slots():
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "bot_add", "type": "any", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert "data" in actual
    assert actual["message"] == "Slot added successfully!"
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0


def test_add_slots_duplicate():
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "bot_add", "type": "any", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot already exists!"
    assert not actual["success"]
    assert actual["error_code"] == 422


def test_add_empty_slots():
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "", "type": "any", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Slot Name cannot be empty or blank spaces"


def test_add_invalid_slots_type():
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "bot_invalid", "type": "invalid", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"][0][
               'msg'] == "value is not a valid enumeration member; permitted: 'float', 'categorical', 'unfeaturized', 'list', 'text', 'bool', 'any'"
    assert not actual["success"]
    assert actual["error_code"] == 422


def test_edit_slots():
    response = client.put(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "bot", "type": "text", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Slot updated!"


def test_edit_empty_slots():
    response = client.put(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "", "type": "any", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Slot Name cannot be empty or blank spaces"


def test_delete_slots():
    client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "bot", "type": "any", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    response = client.delete(
        f"/api/bot/{pytest.bot}/slots/bot",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token}
    )

    actual = response.json()
    assert actual["message"] == "Slot deleted!"
    assert actual["success"]
    assert actual["error_code"] == 0


def test_edit_invalid_slots_type():
    response = client.put(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "bot", "type": "invalid", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0][
               'msg'] == "value is not a valid enumeration member; permitted: 'float', 'categorical', 'unfeaturized', 'list', 'text', 'bool', 'any'"


def test_get_intents():
    response = client.get(
        f"/api/bot/{pytest.bot}/intents",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 14
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_get_all_intents():
    response = client.get(
        f"/api/bot/{pytest.bot}/intents/all",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 14
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_add_intents():
    response = client.post(
        f"/api/bot/{pytest.bot}/intents",
        json={"data": "happier"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Intent added successfully!"


def test_add_intents_duplicate():
    response = client.post(
        f"/api/bot/{pytest.bot}/intents",
        json={"data": "happier"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Intent already exists!"


def test_add_empty_intents():
    response = client.post(
        f"/api/bot/{pytest.bot}/intents",
        json={"data": ""},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Intent Name cannot be empty or blank spaces"


def test_get_training_examples():
    response = client.get(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 8
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_get_training_examples_empty_intent():
    response = client.get(
        f"/api/bot/{pytest.bot}/training_examples/ ",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 0
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_get_training_examples_as_dict(monkeypatch):
    training_examples = {'hi': 'greet', 'hello': 'greet', 'ok': 'affirm', 'no': 'deny'}

    def _mongo_aggregation(*args, **kwargs):
        return [{'training_examples': training_examples}]
    monkeypatch.setattr(BaseQuerySet, 'aggregate', _mongo_aggregation)

    response = client.get(
        f"/api/bot/{pytest.bot}/training_examples",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"] == training_examples
    assert actual["success"]
    assert actual["error_code"] == 0


def test_add_training_examples():
    response = client.post(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        json={"data": ["How do you do?"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"][0]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] is None
    response = client.get(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 9


def test_add_training_examples_duplicate():
    response = client.post(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        json={"data": ["How do you do?"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"][0]["message"] == 'Training Example exists in intent: [\'greet\']'
    assert actual["data"][0]["_id"] is None


def test_add_empty_training_examples():
    response = client.post(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        json={"data": [""]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert (
            actual["data"][0]["message"]
            == "Training Example cannot be empty or blank spaces"
    )
    assert actual["data"][0]["_id"] is None


def test_remove_training_examples():
    training_examples = client.get(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    training_examples = training_examples.json()
    assert len(training_examples["data"]) == 9
    response = client.delete(
        f"/api/bot/{pytest.bot}/training_examples",
        json={"data": training_examples["data"][0]["_id"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Training Example removed!"
    training_examples = client.get(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    training_examples = training_examples.json()
    assert len(training_examples["data"]) == 8


def test_remove_training_examples_empty_id():
    response = client.delete(
        f"/api/bot/{pytest.bot}/training_examples",
        json={"data": ""},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Unable to remove document"


def test_edit_training_examples():
    training_examples = client.get(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    training_examples = training_examples.json()
    response = client.put(
        f"/api/bot/{pytest.bot}/training_examples/greet/" + training_examples["data"][0]["_id"],
        json={"data": "hey, there"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Training Example updated!"


def test_get_responses():
    response = client.get(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 1
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_get_all_responses():
    response = client.get(
        f"/api/bot/{pytest.bot}/response/all",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 14
    assert actual["data"][0]['name']
    assert actual["data"][0]['texts'][0]['text']
    assert not actual["data"][0]['customs']
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_add_response_already_exists():
    response = client.post(
        f"/api/bot/{pytest.bot}/utterance",
        json={"data": "utter_greet"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Utterance exists"


def test_add_utterance_name():
    response = client.post(
        f"/api/bot/{pytest.bot}/utterance",
        json={"data": "utter_test_add_name"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Utterance added!"


def test_add_utterance_name_empty():
    response = client.post(
        f"/api/bot/{pytest.bot}/utterance",
        json={"data": " "},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422


def test_get_utterances():
    response = client.get(
        f"/api/bot/{pytest.bot}/utterance",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual['data']['utterances']) == 15
    assert type(actual['data']['utterances']) == list


def test_add_response():
    response = client.post(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        json={"data": "Wow! How are you?"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Response added!"
    response = client.get(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 2


def test_add_response_upper_case():
    response = client.post(
        f"/api/bot/{pytest.bot}/response/Utter_Greet",
        json={"data": "Upper Greet Response"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Response added!"


def test_get_response_upper_case():
    response = client.get(
        f"/api/bot/{pytest.bot}/response/Utter_Greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 3

    response_lower = client.get(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual_lower = response_lower.json()
    assert len(actual_lower["data"]) == 3
    assert actual_lower["data"] == actual["data"]


def test_add_response_duplicate():
    response = client.post(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        json={"data": "Wow! How are you?"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Utterance already exists!"


def test_add_empty_response():
    response = client.post(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        json={"data": ""},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Utterance text cannot be empty or blank spaces"


def test_remove_response():
    training_examples = client.get(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    training_examples = training_examples.json()
    assert len(training_examples["data"]) == 3
    response = client.delete(
        f"/api/bot/{pytest.bot}/response/False",
        json={"data": training_examples["data"][0]["_id"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Utterance removed!"
    training_examples = client.get(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    training_examples = training_examples.json()
    assert len(training_examples["data"]) == 2


def test_remove_utterance_attached_to_story():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_remove_utterance_attached_to_story",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow added successfully"
    response = client.delete(
        f"/api/bot/{pytest.bot}/response/True",
        json={"data": "utter_greet"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Cannot remove utterance linked to story"


def test_remove_utterance():
    client.post(
        f"/api/bot/{pytest.bot}/response/utter_remove_utterance",
        json={"data": "this will be removed"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    response = client.delete(
        f"/api/bot/{pytest.bot}/response/True",
        json={"data": "utter_remove_utterance"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Utterance removed!"


def test_remove_utterance_non_existing():
    response = client.delete(
        f"/api/bot/{pytest.bot}/response/True",
        json={"data": "utter_delete_non_existing"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Utterance does not exists"


def test_remove_utterance_empty():
    response = client.delete(
        f"/api/bot/{pytest.bot}/response/True",
        json={"data": " "},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Utterance cannot be empty or spaces"


def test_remove_response_empty_id():
    response = client.delete(
        f"/api/bot/{pytest.bot}/response/False",
        json={"data": ""},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Utterance Id cannot be empty or spaces"


def test_remove_response_():
    training_examples = client.get(
        f"/api/bot/{pytest.bot}/response/utter_greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    training_examples = training_examples.json()
    response = client.put(
        f"/api/bot/{pytest.bot}/response/utter_greet/" + training_examples["data"][0]["_id"],
        json={"data": "Hello, How are you!"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Utterance updated!"


def test_add_story():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "test_greet", "type": "INTENT"},
                {"name": "utter_test_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["message"] == "Flow added successfully"
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0


def test_add_story_invalid_type():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "TEST",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [{'ctx': {'enum_values': ['STORY', 'RULE']}, 'loc': ['body', 'type'],
                                  'msg': "value is not a valid enumeration member; permitted: 'STORY', 'RULE'",
                                  'type': 'type_error.enum'}]


def test_add_story_empty_event():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={"name": "test_add_story_empty_event", "type": "STORY", "steps": []},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'steps'], 'msg': 'Steps are required to form Flow', 'type': 'value_error'}]


def test_add_story_lone_intent():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_story_lone_intent",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
                {"name": "greet_again", "type": "INTENT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'steps'], 'msg': 'Intent should be followed by utterance or action', 'type': 'value_error'}]


def test_add_story_consecutive_intents():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_story_consecutive_intents",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'steps'], 'msg': 'Found 2 consecutive intents', 'type': 'value_error'}]


def test_add_story_multiple_actions():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_story_consecutive_actions",
            "type": "STORY",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "HTTP_ACTION"},
                {"name": "utter_greet_again", "type": "HTTP_ACTION"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow added successfully"


def test_add_story_utterance_as_first_step():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_story_consecutive_intents",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "BOT"},
                {"name": "utter_greet", "type": "HTTP_ACTION"},
                {"name": "utter_greet_again", "type": "HTTP_ACTION"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'steps'], 'msg': 'First step should be an intent', 'type': 'value_error'}]


def test_add_story_missing_event_type():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [{"name": "greet"}, {"name": "utter_greet", "type": "BOT"}],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert (
            actual["message"]
            == [{'loc': ['body', 'steps', 0, 'type'], 'msg': 'field required', 'type': 'value_error.missing'}]
    )


def test_add_story_invalid_event_type():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "data"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert (
            actual["message"]
            == [{'ctx': {'enum_values': ['INTENT', 'BOT', 'HTTP_ACTION', 'ACTION', 'SLOT_SET_ACTION']},
                 'loc': ['body', 'steps', 0, 'type'],
                 'msg': "value is not a valid enumeration member; permitted: 'INTENT', 'BOT', 'HTTP_ACTION', 'ACTION', 'SLOT_SET_ACTION'",
                 'type': 'type_error.enum'}]
    )


def test_update_story():
    response = client.put(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_nonsense", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["message"] == "Flow updated successfully"
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0


def test_update_story_invalid_event_type():
    response = client.put(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "data"},
                {"name": "utter_nonsense", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert (
            actual["message"]
            == [{'ctx': {'enum_values': ['INTENT', 'BOT', 'HTTP_ACTION', 'ACTION', 'SLOT_SET_ACTION']},
                 'loc': ['body', 'steps', 0, 'type'],
                 'msg': "value is not a valid enumeration member; permitted: 'INTENT', 'BOT', 'HTTP_ACTION', 'ACTION', 'SLOT_SET_ACTION'",
                 'type': 'type_error.enum'}]
    )


def test_delete_story():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path1",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet_delete", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["message"] == "Flow added successfully"
    assert actual["success"]
    assert actual["error_code"] == 0

    response = client.delete(
        f"/api/bot/{pytest.bot}/stories/test_path1/STORY",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow deleted successfully"


def test_delete_non_existing_story():
    response = client.delete(
        f"/api/bot/{pytest.bot}/stories/test_path2/STORY",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Flow does not exists"


def test_get_stories():
    response = client.get(
        f"/api/bot/{pytest.bot}/stories",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]
    assert Utility.check_empty_string(actual["message"])
    assert actual["data"][0]['template_type'] == 'CUSTOM'
    assert actual["data"][1]['template_type'] == 'CUSTOM'
    assert actual["data"][16]['template_type'] == 'Q&A'
    assert actual["data"][17]['template_type'] == 'Q&A'
    assert not actual["data"][19].get('template_type')


def test_get_utterance_from_intent():
    response = client.get(
        f"/api/bot/{pytest.bot}/utterance_from_intent/greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]["name"] == "utter_offer_help"
    assert actual["data"]["type"] == UTTERANCE_TYPE.BOT
    assert Utility.check_empty_string(actual["message"])


def test_get_utterance_from_not_exist_intent():
    response = client.get(
        f"/api/bot/{pytest.bot}/utterance_from_intent/greeting",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]["name"] is None
    assert actual["data"]["type"] is None
    assert Utility.check_empty_string(actual["message"])


def test_train_on_updated_data(monkeypatch):
    def mongo_store(*arge, **kwargs):
        return None

    def _mock_training_limit(*arge, **kwargs):
        return False

    monkeypatch.setattr(Utility, "get_local_mongo_store", mongo_store)
    monkeypatch.setattr(ModelProcessor, "is_daily_training_limit_exceeded", _mock_training_limit)

    response = client.post(
        f"/api/bot/{pytest.bot}/train",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Model training started."


@pytest.fixture
def mock_is_training_inprogress_exception(monkeypatch):
    def _inprogress_execption_response(*args, **kwargs):
        raise AppException("Previous model training in progress.")

    monkeypatch.setattr(ModelProcessor, "is_training_inprogress", _inprogress_execption_response)


def test_train_inprogress(mock_is_training_inprogress_exception):
    response = client.post(
        f"/api/bot/{pytest.bot}/train",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"] == False
    assert actual["error_code"] == 422
    assert actual["data"] is None
    assert actual["message"] == "Previous model training in progress."


@pytest.fixture
def mock_is_training_inprogress(monkeypatch):
    def _inprogress_response(*args, **kwargs):
        return False

    monkeypatch.setattr(ModelProcessor, "is_training_inprogress", _inprogress_response)


def test_train_daily_limit_exceed(mock_is_training_inprogress):
    response = client.post(
        f"/api/bot/{pytest.bot}/train",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["data"] is None
    assert actual["message"] == "Daily model training limit exceeded."


def test_get_model_training_history():
    response = client.get(
        f"/api/bot/{pytest.bot}/train/history",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"] is True
    assert actual["error_code"] == 0
    assert actual["data"]
    assert "training_history" in actual["data"]


def test_get_file_training_history():
    response = client.get(
        f"/api/bot/{pytest.bot}/data/generation/history",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"] is True
    assert actual["error_code"] == 0
    assert actual["data"]
    assert "training_history" in actual["data"]


def test_deploy_missing_configuration():
    response = client.post(
        f"/api/bot/{pytest.bot}/deploy",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Please configure the bot endpoint for deployment!"


def endpoint_response(*args, **kwargs):
    return {"bot_endpoint": {"url": "http://localhost:5000"}}


@pytest.fixture
def mock_endpoint(monkeypatch):
    monkeypatch.setattr(MongoProcessor, "get_endpoints", endpoint_response)


@pytest.fixture
def mock_endpoint_with_token(monkeypatch):
    def _endpoint_response(*args, **kwargs):
        return {
            "bot_endpoint": {
                "url": "http://localhost:5000",
                "token": "AGTSUDH!@#78JNKLD",
                "token_type": "Bearer",
            }
        }

    monkeypatch.setattr(MongoProcessor, "get_endpoints", _endpoint_response)


def test_deploy_connection_error(mock_endpoint):
    response = client.post(
        f"/api/bot/{pytest.bot}/deploy",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Host is not reachable"


@responses.activate
def test_deploy(mock_endpoint):
    responses.add(
        responses.PUT,
        "http://localhost:5000/model",
        status=204,
    )
    response = client.post(
        f"/api/bot/{pytest.bot}/deploy",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Model was successfully replaced."


@responses.activate
def test_deployment_history():
    response = client.get(
        f"/api/bot/{pytest.bot}/deploy/history",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual["data"]['deployment_history']) == 3
    assert actual["message"] is None


@responses.activate
def test_deploy_with_token(mock_endpoint_with_token):
    responses.add(
        responses.PUT,
        "http://localhost:5000/model",
        json="Model was successfully replaced.",
        headers={"Content-type": "application/json",
                 "Accept": "text/plain",
                 "Authorization": "Bearer AGTSUDH!@#78JNKLD"},
        status=200,
    )
    response = client.post(
        f"/api/bot/{pytest.bot}/deploy",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Model was successfully replaced."


@responses.activate
def test_deploy_bad_request(mock_endpoint):
    responses.add(
        responses.PUT,
        "http://localhost:5000/model",
        json={
            "version": "1.0.0",
            "status": "failure",
            "reason": "BadRequest",
            "code": 400,
        },
        status=200,
    )
    response = client.post(
        f"/api/bot/{pytest.bot}/deploy",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "BadRequest"


@responses.activate
def test_deploy_server_error(mock_endpoint):
    responses.add(
        responses.PUT,
        "http://localhost:5000/model",
        json={
            "version": "1.0.0",
            "status": "ServerError",
            "message": "An unexpected error occurred.",
            "code": 500,
        },
        status=200,
    )
    response = client.post(
        f"/api/bot/{pytest.bot}/deploy",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "An unexpected error occurred."


def test_integration_token():
    response = client.get(
        f"/api/auth/{pytest.bot}/integration/token",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    token = response.json()
    assert token["success"]
    assert token["error_code"] == 0
    assert token["data"]["access_token"]
    assert token["data"]["token_type"]
    assert (
            token["message"]
            == """It is your responsibility to keep the token secret.
        If leaked then other may have access to your system."""
    )
    response = client.get(
        f"/api/bot/{pytest.bot}/intents",
        headers={
            "Authorization": token["data"]["token_type"]
                             + " "
                             + token["data"]["access_token"],
            "X-USER": "integration",
        },
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 15
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])
    response = client.post(
        f"/api/bot/{pytest.bot}/intents",
        headers={
            "Authorization": token["data"]["token_type"]
                             + " "
                             + token["data"]["access_token"],
            "X-USER": "integration",
        },
        json={"data": "integration"},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Intent added successfully!"


def test_integration_token_missing_x_user():
    response = client.get(
        f"/api/auth/{pytest.bot}/integration/token",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]["access_token"]
    assert actual["data"]["token_type"]
    assert (
            actual["message"]
            == """It is your responsibility to keep the token secret.
        If leaked then other may have access to your system."""
    )
    response = client.get(
        f"/api/bot/{pytest.bot}/intents",
        headers={
            "Authorization": actual["data"]["token_type"]
                             + " "
                             + actual["data"]["access_token"]
        },
    )
    actual = response.json()
    assert actual["data"] is None
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Alias user missing for integration"


@responses.activate
def test_augment_paraphrase_gpt():
    responses.add(
        responses.POST,
        url="http://localhost:8000/paraphrases/gpt",
        match=[responses.json_params_matcher(
            {"api_key": "MockKey", "data": ["Where is digite located?"], "engine": "davinci", "temperature": 0.75,
             "max_tokens": 100, "num_responses": 10})],
        json={
            "success": True,
            "data": {
                "paraphrases": ['Where is digite located?',
                                'Where is digite situated?']
            },
            "message": None,
            "error_code": 0,
        },
        status=200
    )
    response = client.post(
        "/api/augment/paraphrases/gpt",
        json={"data": ["Where is digite located?"], "api_key": "MockKey"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()

    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] == {
        "paraphrases": ['Where is digite located?',
                        'Where is digite situated?']
    }
    assert Utility.check_empty_string(actual["message"])


def test_augment_paraphrase_gpt_validation():
    response = client.post(
        "/api/augment/paraphrases/gpt",
        json={"data": [], "api_key": "MockKey"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["data"] is None
    assert actual["message"] == [{'loc': ['body', 'data'], 'msg': 'Question Please!', 'type': 'value_error'}]

    response = client.post(
        "/api/augment/paraphrases/gpt",
        json={"data": ["hi", "hello", "thanks", "hello", "bye", "how are you"], "api_key": "MockKey"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["data"] is None
    assert actual["message"] == [
        {'loc': ['body', 'data'], 'msg': 'Max 5 Questions are allowed!', 'type': 'value_error'}]


@responses.activate
def test_augment_paraphrase_gpt_fail():
    key_error_message = "Incorrect API key provided: InvalidKey. You can find your API key at https://beta.openai.com."
    responses.add(
        responses.POST,
        url="http://localhost:8000/paraphrases/gpt",
        match=[responses.json_params_matcher(
            {"api_key": "InvalidKey", "data": ["Where is digite located?"], "engine": "davinci", "temperature": 0.75,
             "max_tokens": 100, "num_responses": 10})],
        json={
            "success": False,
            "data": None,
            "message": key_error_message,
        },
        status=200,
    )
    response = client.post(
        "/api/augment/paraphrases/gpt",
        json={"data": ["Where is digite located?"], "api_key": "InvalidKey"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()

    assert not actual["success"]
    assert actual["data"] is None
    assert actual["message"] == key_error_message


@responses.activate
def test_augment_paraphrase():
    responses.add(
        responses.POST,
        "http://localhost:8000/paraphrases",
        json={
            "success": True,
            "data": {
                "questions": ['Where is digite located?',
                              'Where is digite?',
                              'What is the location of digite?',
                              'Where is the digite located?',
                              'Where is it located?',
                              'What location is digite located?',
                              'Where is the digite?',
                              'where is digite located?',
                              'Where is digite situated?',
                              'digite is located where?']
            },
            "message": None,
            "error_code": 0,
        },
        status=200,
        match=[responses.json_params_matcher(["where is digite located?"])]
    )
    response = client.post(
        "/api/augment/paraphrases",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"data": ["where is digite located?"]},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]
    assert Utility.check_empty_string(actual["message"])


def test_augment_paraphrase_no_of_questions():
    response = client.post(
        "/api/augment/paraphrases",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"data": []},
    )

    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["data"] is None
    assert actual["message"] == [{'loc': ['body', 'data'], 'msg': 'Question Please!', 'type': 'value_error'}]

    response = client.post(
        "/api/augment/paraphrases",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"data": ["Hi", "Hello", "How are you", "Bye", "Thanks", "Welcome"]},
    )

    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["data"] is None
    assert actual["message"] == [
        {'loc': ['body', 'data'], 'msg': 'Max 5 Questions are allowed!', 'type': 'value_error'}]


def test_get_user_details():
    response = client.get(
        "/api/user/details",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]
    assert Utility.check_empty_string(actual["message"])


def test_download_data():
    response = client.get(
        f"/api/bot/{pytest.bot}/download/data",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    file_bytes = BytesIO(response.content)
    zip_file = ZipFile(file_bytes, mode='r')
    assert zip_file.filelist.__len__()
    zip_file.close()
    file_bytes.close()


def test_download_model():
    response = client.get(
        f"/api/bot/{pytest.bot}/download/model",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    d = response.headers['content-disposition']
    fname = re.findall("filename=(.+)", d)[0]
    file_bytes = BytesIO(response.content)
    tar = tarfile.open(fileobj=file_bytes, mode='r', name=fname)
    assert tar.members.__len__()
    tar.close()
    file_bytes.close()


def test_get_endpoint():
    response = client.get(
        f"/api/bot/{pytest.bot}/endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual['data']
    assert actual['error_code'] == 0
    assert actual['message'] is None
    assert actual['success']


def test_save_endpoint_error():
    response = client.put(
        f"/api/bot/{pytest.bot}/endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 422
    assert actual['message'] == [{'loc': ['body'], 'msg': 'field required', 'type': 'value_error.missing'}]
    assert not actual['success']


def test_save_empty_endpoint():
    response = client.put(
        f"/api/bot/{pytest.bot}/endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == 'Endpoint saved successfully!'
    assert actual['success']


def test_save_history_endpoint():
    response = client.put(
        f"/api/bot/{pytest.bot}/endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"history_endpoint": {
            "url": "http://localhost:27019/",
            "token": "kairon-history-user",
        }}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == 'Endpoint saved successfully!'
    assert actual['success']


@responses.activate
def test_save_endpoint(monkeypatch):
    def mongo_store(*args, **kwargs):
        return None

    monkeypatch.setattr(Utility, "get_local_mongo_store", mongo_store)
    monkeypatch.setitem(Utility.environment['action'], "url", None)
    monkeypatch.setitem(Utility.environment['model']['agent'], "url", "http://localhost/")

    responses.add(
        responses.GET,
        f"http://localhost/api/bot/{pytest.bot}/reload",
        status=200,
        json={'success': True, 'error_code': 0, "data": None, 'message': "Reloading Model!"}
    )

    response = client.put(
        f"/api/bot/{pytest.bot}/endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"bot_endpoint": {"url": "http://localhost:5005/"},
              "action_endpoint": {"url": "http://localhost:5000/"},
              "history_endpoint": {"url": "http://localhost", "token": "rasa234568"}}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == 'Endpoint saved successfully!'
    assert actual['success']
    response = client.get(
        f"/api/bot/{pytest.bot}/endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual['data']['endpoint'].get('bot_endpoint')
    assert actual['data']['endpoint'].get('action_endpoint')
    assert actual['data']['endpoint'].get('history_endpoint')


def test_save_empty_history_endpoint():
    response = client.put(
        f"/api/bot/{pytest.bot}/endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"history_endpoint": {
            "url": " ",
            "token": "testing-endpoint"
        }}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 422
    assert actual['message'] == 'url cannot be blank or empty spaces'
    assert not actual['success']


def test_get_history_endpoint():
    response = client.get(
        f"/api/bot/{pytest.bot}/endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual['data']['endpoint']['history_endpoint']['url'] == "http://localhost"
    assert actual['data']['endpoint']['history_endpoint']['token'] == "rasa234***"
    assert actual['error_code'] == 0
    assert actual['message'] is None
    assert actual['success']


def test_delete_endpoint():
    response = client.delete(
        f"/api/bot/{pytest.bot}/endpoint/history_endpoint",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == 'Endpoint removed'
    assert actual['success']


def test_get_templates():
    response = client.get(
        f"/api/bot/{pytest.bot}/templates/use-case",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert "Hi-Hello" in actual['data']['use-cases']
    assert actual['error_code'] == 0
    assert actual['message'] is None
    assert actual['success']


def test_set_templates():
    response = client.post(
        f"/api/bot/{pytest.bot}/templates/use-case",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"data": "Hi-Hello"}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == "Data applied!"
    assert actual['success']


def test_set_templates_invalid():
    response = client.post(
        f"/api/bot/{pytest.bot}/templates/use-case",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"data": "Hi"}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 422
    assert actual['message'] == "Invalid template!"
    assert not actual['success']


@responses.activate
def test_reload_model(monkeypatch):
    def mongo_store(*arge, **kwargs):
        return None

    monkeypatch.setattr(Utility, "get_local_mongo_store", mongo_store)
    monkeypatch.setitem(Utility.environment['action'], "url", None)
    monkeypatch.setitem(Utility.environment['model']['agent'], "url", "http://localhost/")

    responses.add(
        responses.GET,
        f"http://localhost/api/bot/{pytest.bot}/reload",
        status=200,
        json={'success': True, 'error_code': 0, "data": None, 'message': "Reloading Model!"}
    )

    response = client.get(
        f"/api/bot/{pytest.bot}/model/reload",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == "Reloading Model!"
    assert actual['success']


def test_get_config_templates():
    response = client.get(
        f"/api/bot/{pytest.bot}/templates/config",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert any("default" == template['name'] for template in actual['data']['config-templates'])
    assert actual['error_code'] == 0
    assert actual['message'] is None
    assert actual['success']


def test_set_config_templates():
    response = client.post(
        f"/api/bot/{pytest.bot}/templates/config",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"data": "default"}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == "Config applied!"
    assert actual['success']


def test_set_config_templates_invalid():
    response = client.post(
        f"/api/bot/{pytest.bot}/templates/config",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"data": "test"}
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 422
    assert actual['message'] == "Invalid config!"
    assert not actual['success']


def test_get_config():
    response = client.get(
        f"/api/bot/{pytest.bot}/config",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert all(key in ["language", "pipeline", "policies"] for key in actual['data']['config'].keys())
    assert actual['error_code'] == 0
    assert actual['message'] is None
    assert actual['success']


def test_set_config():
    response = client.put(
        f"/api/bot/{pytest.bot}/config",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json=read_config_file('./template/config/default.yml')
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == "Config saved!"
    assert actual['success']


def test_set_config_policy_error():
    data = read_config_file('./template/config/default.yml')
    data['policies'].append({"name": "TestPolicy"})
    response = client.put(
        f"/api/bot/{pytest.bot}/config",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json=data
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 422
    assert actual[
               'message'] == "Invalid policy TestPolicy"
    assert not actual['success']


def test_set_config_pipeline_error():
    data = read_config_file('./template/config/default.yml')
    data['pipeline'].append({"name": "TestFeaturizer"})
    response = client.put(
        f"/api/bot/{pytest.bot}/config",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json=data
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 422
    assert str(actual['message']).__contains__("Invalid component TestFeaturizer")
    assert not actual['success']


def test_set_config_pipeline_error_empty_policies():
    data = read_config_file('./template/config/default.yml')
    data['policies'] = []
    response = client.put(
        f"/api/bot/{pytest.bot}/config",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json=data
    )

    actual = response.json()
    assert actual['data'] is None
    assert str(actual['message']).__contains__("You didn't define any policies")
    assert actual['error_code'] == 422
    assert not actual['success']


def test_delete_intent():
    client.post(
        f"/api/bot/{pytest.bot}/intents",
        json={"data": "happier"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    response = client.delete(
        f"/api/bot/{pytest.bot}/intents/happier/True",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == "Intent deleted!"
    assert actual['success']


def test_api_login_with_account_not_verified():
    Utility.email_conf["email"]["enable"] = True
    response = client.post(
        "/api/auth/login",
        data={"username": "integration@demo.ai", "password": "Welcome@1"},
    )
    actual = response.json()
    Utility.email_conf["email"]["enable"] = False

    assert not actual['success']
    assert actual['error_code'] == 422
    assert actual['data'] is None
    assert actual['message'] == 'Please verify your mail'


async def mock_smtp(*args, **kwargs):
    return None


def test_account_registration_with_confirmation(monkeypatch):
    monkeypatch.setattr(Utility, 'trigger_smtp', mock_smtp)
    Utility.email_conf["email"]["enable"] = True
    response = client.post(
        "/api/account/registration",
        json={
            "email": "integ1@gmail.com",
            "first_name": "Dem",
            "last_name": "User22",
            "password": "Welcome@1",
            "confirm_password": "Welcome@1",
            "account": "integration33",
            "bot": "integration33",
        },
    )
    actual = response.json()
    assert actual["message"] == "Account Registered! A confirmation link has been sent to your mail"
    assert actual['success']
    assert actual['error_code'] == 0
    assert actual['data'] is None

    response = client.post("/api/account/email/confirmation",
                           json={
                               'data': 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJtYWlsX2lkIjoiaW50ZWcxQGdtYWlsLmNvbSJ9.Ycs1ROb1w6MMsx2WTA4vFu3-jRO8LsXKCQEB3fkoU20'},
                           )
    actual = response.json()
    Utility.email_conf["email"]["enable"] = False

    assert actual['message'] == "Account Verified!"
    assert actual['data'] is None
    assert actual['success']
    assert actual['error_code'] == 0


def test_invalid_token_for_confirmation():
    response = client.post("/api/account/email/confirmation",
                           json={
                               'data': 'hello'},
                           )
    actual = response.json()

    assert actual['message'] == "Invalid token"
    assert actual['data'] is None
    assert not actual['success']
    assert actual['error_code'] == 422


def test_add_intents_no_bot():
    response = client.post(
        "/api/bot/ /intents",
        json={"data": "greet"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Bot is required'


def test_add_intents_not_authorised():
    response = client.post(
        "/api/bot/5ea8127db7c285f4055129a4/intents",
        json={"data": "greet"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Access denied for bot'


def test_add_intents_inactive_bot(monkeypatch):
    def _mock_bot(*args, **kwargs):
        return {'status': False}

    async def _mock_user(*args, **kwargs):
        return User(
            email='test',
            first_name='test',
            last_name='test',
            account=2,
            status=True,
            is_integration_user=False,
            bot=['5ea8127db7c285f4055129a4', '5ea8127db7c285f4055129a5'])

    monkeypatch.setattr(AccountProcessor, 'get_bot', _mock_bot)
    monkeypatch.setattr(Authentication, 'get_current_user', _mock_user)

    response = client.post(
        "/api/bot/5ea8127db7c285f4055129a4/intents",
        json={"data": "greet"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Inactive Bot Please contact system admin!'


def test_add_intents_invalid_auth_token():
    token = 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuYW1lIjoiSm9obiBEb2UiLCJpYXQiOjE1MTYyMzkwMjJ9.hqWGSaFpvbrXkOWc6lrnffhNWR19W_S1YKFBx2arWBk'
    response = client.post(
        "/api/bot/ /intents",
        json={"data": "greet"},
        headers={"Authorization": token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 401
    assert actual["message"] == "Could not validate credentials"


def test_add_intents_invalid_auth_token_2():
    token = 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
    response = client.post(
        "/api/bot/ /intents",
        json={"data": "greet"},
        headers={"Authorization": token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 401
    assert actual["message"] == "Could not validate credentials"


def test_add_intents_to_different_bot():
    response = client.get(
        "/api/account/bot",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    pytest.bot_2 = response['data'][1]['_id']

    response = client.post(
        f"/api/bot/{pytest.bot_2}/intents",
        json={"data": "greet"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Intent added successfully!"


def test_add_training_examples_to_different_bot():
    response = client.post(
        f"/api/bot/{pytest.bot_2}/training_examples/greet",
        json={"data": ["Hi"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"][0]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] is None
    response = client.get(
        f"/api/bot/{pytest.bot_2}/training_examples/greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 1


def test_add_response_different_bot():
    response = client.post(
        f"/api/bot/{pytest.bot_2}/response/utter_greet",
        json={"data": "Hi! How are you?"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Response added!"
    response = client.get(
        f"/api/bot/{pytest.bot_2}/response/utter_greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 1


def test_add_story_to_different_bot():
    response = client.post(
        f"/api/bot/{pytest.bot_2}/stories",
        json={
            "name": "greet user",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["message"] == "Flow added successfully"
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0


def test_train_on_different_bot(monkeypatch):
    def mongo_store(*arge, **kwargs):
        return None

    def _mock_training_limit(*arge, **kwargs):
        return False

    monkeypatch.setattr(Utility, "get_local_mongo_store", mongo_store)
    monkeypatch.setattr(ModelProcessor, "is_daily_training_limit_exceeded", _mock_training_limit)

    response = client.post(
        f"/api/bot/{pytest.bot_2}/train",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Model training started."


def test_delete_bot():
    response = client.get(
        "/api/account/bot",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    bot = response['data'][1]['_id']

    response = client.delete(
        f"/api/account/bot/{bot}",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    assert response['message'] == 'Bot removed'
    assert response['error_code'] == 0
    assert response['success']


def test_login_for_verified():
    Utility.email_conf["email"]["enable"] = True
    response = client.post(
        "/api/auth/login",
        data={"username": "integ1@gmail.com", "password": "Welcome@1"},
    )
    actual = response.json()
    Utility.email_conf["email"]["enable"] = False

    assert actual["success"]
    assert actual["error_code"] == 0
    pytest.access_token = actual["data"]["access_token"]
    pytest.token_type = actual["data"]["token_type"]


def test_list_bots_for_different_user():
    response = client.get(
        "/api/account/bot",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    assert len(response['data']) == 1
    pytest.bot = response['data'][0]['_id']


def test_reset_password_for_valid_id(monkeypatch):
    monkeypatch.setattr(Utility, 'trigger_smtp', mock_smtp)
    Utility.email_conf["email"]["enable"] = True
    response = client.post(
        "/api/account/password/reset",
        json={"data": "integ1@gmail.com"},
    )
    actual = response.json()
    Utility.email_conf["email"]["enable"] = False
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Success! A password reset link has been sent to your mail id"
    assert actual['data'] is None


def test_reset_password_for_invalid_id():
    Utility.email_conf["email"]["enable"] = True
    response = client.post(
        "/api/account/password/reset",
        json={"data": "sasha.41195@gmail.com"},
    )
    actual = response.json()
    Utility.email_conf["email"]["enable"] = False
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Error! There is no user with the following mail id"
    assert actual['data'] is None


def test_overwrite_password_for_matching_passwords(monkeypatch):
    monkeypatch.setattr(Utility, 'trigger_smtp', mock_smtp)
    response = client.post(
        "/api/account/password/change",
        json={
            "data": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJtYWlsX2lkIjoiaW50ZWcxQGdtYWlsLmNvbSJ9.Ycs1ROb1w6MMsx2WTA4vFu3-jRO8LsXKCQEB3fkoU20",
            "password": "Welcome@2",
            "confirm_password": "Welcome@2"},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Success! Your password has been changed"
    assert actual['data'] is None


def test_login_new_password():
    response = client.post(
        "/api/auth/login",
        data={"username": "integ1@gmail.com", "password": "Welcome@2"},
    )
    actual = response.json()

    assert actual["success"]
    assert actual["error_code"] == 0
    pytest.access_token = actual["data"]["access_token"]
    pytest.token_type = actual["data"]["token_type"]


def test_list_bots_for_different_user_2():
    response = client.get(
        "/api/account/bot",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    ).json()
    assert len(response['data']) == 1
    pytest.bot = response['data'][0]['_id']


def test_login_old_password():
    response = client.post(
        "/api/auth/login",
        data={"username": "integ1@gmail.com", "password": "Welcome@1"},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 401
    assert actual["message"] == 'Incorrect username or password'
    assert actual['data'] is None


def test_send_link_for_valid_id(monkeypatch):
    monkeypatch.setattr(Utility, 'trigger_smtp', mock_smtp)
    Utility.email_conf["email"]["enable"] = True
    response = client.post("/api/account/email/confirmation/link",
                           json={
                               'data': 'integration@demo.ai'},
                           )
    actual = response.json()
    Utility.email_conf["email"]["enable"] = False
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == 'Success! Confirmation link sent'
    assert actual['data'] is None


def test_send_link_for_confirmed_id():
    Utility.email_conf["email"]["enable"] = True
    response = client.post("/api/account/email/confirmation/link",
                           json={
                               'data': 'integ1@gmail.com'},
                           )
    actual = response.json()
    Utility.email_conf["email"]["enable"] = False
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Email already confirmed!'
    assert actual['data'] is None


def test_overwrite_password_for_non_matching_passwords():
    Utility.email_conf["email"]["enable"] = True
    response = client.post(
        "/api/account/password/change",
        json={
            "data": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJtYWlsX2lkIjoiaW50ZWcxQGdtYWlsLmNvbSJ9.Ycs1ROb1w6MMsx2WTA4vFu3-jRO8LsXKCQEB3fkoU20",
            "password": "Welcome@2",
            "confirm_password": "Welcume@2"},
    )
    actual = response.json()
    Utility.email_conf["email"]["enable"] = False
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual['data'] is None


def test_add_and_delete_intents_by_integration_user():
    response = client.get(
        f"/api/auth/{pytest.bot}/integration/token",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    token = response.json()
    assert token["success"]
    assert token["error_code"] == 0
    assert token["data"]["access_token"]
    assert token["data"]["token_type"]

    response = client.post(
        f"/api/bot/{pytest.bot}/intents",
        headers={
            "Authorization": token["data"]["token_type"]
                             + " "
                             + token["data"]["access_token"],
            "X-USER": "integration",
        },
        json={"data": "integration_intent"},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Intent added successfully!"

    response = client.delete(
        f"/api/bot/{pytest.bot}/intents/integration_intent/True",
        headers={
            "Authorization": token["data"]["token_type"]
                             + " "
                             + token["data"]["access_token"],
            "X-USER": "integration1",
        },
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 0
    assert actual['message'] == "Intent deleted!"
    assert actual['success']


def test_add_non_Integration_Intent_and_delete_intent_by_integration_user():
    response = client.get(
        f"/api/auth/{pytest.bot}/integration/token",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    token = response.json()
    assert token["success"]
    assert token["error_code"] == 0
    assert token["data"]["access_token"]
    assert token["data"]["token_type"]

    response = client.post(
        f"/api/bot/{pytest.bot}/intents",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json={"data": "non_integration_intent"},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Intent added successfully!"

    response = client.delete(
        f"/api/bot/{pytest.bot}/intents/non_integration_intent/True",
        headers={
            "Authorization": token["data"]["token_type"]
                             + " "
                             + token["data"]["access_token"],
            "X-USER": "integration1",
        },
    )

    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 422
    assert actual['message'] == "This intent cannot be deleted by an integration user"
    assert not actual['success']


def test_add_http_action_malformed_url():
    request_body = {
        "auth_token": "",
        "action_name": "new_http_action",
        "response": "",
        "http_url": "192.168.104.1/api/test",
        "request_method": "GET",
        "http_params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }
    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_add_http_action_missing_parameters():
    request_body = {
        "action_name": "new_http_action2",
        "response": "",
        "http_url": "http://www.google.com",
        "request_method": "put",
        "params_list": [{
            "key": "",
            "parameter_type": "",
            "value": ""
        }]
    }
    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_add_http_action_invalid_req_method():
    request_body = {
        "auth_token": "",
        "action_name": "new_http_action",
        "response": "",
        "http_url": "http://www.google.com",
        "request_method": "TUP",
        "http_params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }
    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_add_http_action_no_action_name():
    request_body = {
        "auth_token": "",
        "action_name": "",
        "response": "string",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "http_params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_add_http_action_no_token():
    request_body = {
        "auth_token": "",
        "action_name": "test_add_http_action_no_token",
        "response": "string",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "http_params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["message"]
    assert actual["success"]


def test_add_http_action_with_sender_id_parameter_type():
    request_body = {
        "auth_token": "",
        "action_name": "test_add_http_action_with_sender_id_parameter_type",
        "response": "string",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": [{
            "key": "testParam1",
            "parameter_type": "sender_id",
            "value": "testValue1"
        }, {
            "key": "testParam2",
            "parameter_type": "slot",
            "value": "testValue2"
        }, {
            "key": "testParam3",
            "parameter_type": "user_message",
        }],
        "headers": [{
            "key": "testParam1",
            "parameter_type": "sender_id",
            "value": "testValue1"
        }, {
            "key": "testParam2",
            "parameter_type": "slot",
            "value": "testValue2"
        }, {
            "key": "testParam3",
            "parameter_type": "user_message",
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["message"]
    assert actual["success"]


def test_get_http_action():
    response = client.get(
        url=f"/api/bot/{pytest.bot}/action/httpaction/test_add_http_action_with_sender_id_parameter_type",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    print(actual)
    assert actual["error_code"] == 0
    assert actual["data"]['action_name'] == 'test_add_http_action_with_sender_id_parameter_type'
    assert actual["data"]['response'] == 'string'
    assert actual["data"]['http_url'] == 'http://www.google.com'
    assert actual["data"]['request_method'] == 'GET'
    assert actual["data"]['params_list'] == [
        {'key': 'testParam1', 'value': 'testValue1', 'parameter_type': 'sender_id'},
        {'key': 'testParam2', 'value': 'testValue2', 'parameter_type': 'slot'},
        {'key': 'testParam3', 'value': '', 'parameter_type': 'user_message'}]
    assert actual["data"]['headers'] == [
        {'key': 'testParam1', 'value': 'testValue1', 'parameter_type': 'sender_id'},
        {'key': 'testParam2', 'value': 'testValue2', 'parameter_type': 'slot'},
        {'key': 'testParam3', 'value': '', 'parameter_type': 'user_message'}]
    assert not actual["message"]
    assert actual["success"]


def test_add_http_action_invalid_parameter_type():
    request_body = {
        "auth_token": "",
        "action_name": "test_add_http_action_invalid_parameter_type",
        "response": "string",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": [{
            "key": "testParam1",
            "parameter_type": "val",
            "value": "testValue1"
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_add_http_action_with_token():
    request_body = {
        "action_name": "test_add_http_action_with_token_and_story",
        "response": "string",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "headers": [{
            "key": "Authorization",
            "parameter_type": "value",
            "value": "bearer dfiuhdfishifoshfoishnfoshfnsifjfs"
        }, {
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }, {
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["message"]
    assert actual["success"]

    response = client.get(
        url=f"/api/bot/{pytest.bot}/action/httpaction/test_add_http_action_with_token_and_story",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual['data']["response"] == "string"
    assert actual['data']["headers"] == request_body['headers']
    assert actual['data']["http_url"] == "http://www.google.com"
    assert actual['data']["request_method"] == "GET"
    assert len(actual['data']["headers"]) == 3
    assert actual["success"]


def test_add_http_action_no_params():
    request_body = {
        "auth_token": "",
        "action_name": "test_add_http_action_no_params",
        "response": "string",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": []
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["message"]
    assert actual["success"]


def test_add_http_action_existing():
    request_body = {
        "auth_token": "",
        "action_name": "test_add_http_action_existing",
        "response": "string",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_get_http_action_non_exisitng():
    response = client.get(
        url=f"/api/bot/{pytest.bot}/action/httpaction/never_added",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"] is not None
    assert not actual["success"]


def test_update_http_action():
    request_body = {
        "action_name": "test_update_http_action",
        "response": "",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0

    request_body = {
        "action_name": "test_update_http_action",
        "response": "json",
        "http_url": "http://www.alphabet.com",
        "request_method": "POST",
        "params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }, {
            "key": "testParam2",
            "parameter_type": "slot",
            "value": "testValue1"
        }],
        "headers": [{
            "key": "Authorization",
            "parameter_type": "value",
            "value": "bearer token"
        }]
    }
    response = client.put(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0

    response = client.get(
        url=f"/api/bot/{pytest.bot}/action/httpaction/test_update_http_action",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual['data']["response"] == "json"
    assert actual['data']["http_url"] == "http://www.alphabet.com"
    assert actual['data']["request_method"] == "POST"
    assert len(actual['data']["params_list"]) == 2
    assert actual['data']["params_list"][0]['key'] == 'testParam1'
    assert actual['data']["params_list"][0]['parameter_type'] == 'value'
    assert actual['data']["params_list"][0]['value'] == 'testValue1'
    assert actual['data']["headers"] == request_body['headers']
    assert actual["success"]


def test_update_http_action_wrong_parameter():
    request_body = {
        "auth_token": "",
        "action_name": "test_update_http_action_6",
        "response": "",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0

    request_body = {
        "auth_token": "bearer hjklfsdjsjkfbjsbfjsvhfjksvfjksvfjksvf",
        "action_name": "test_update_http_action_6",
        "response": "json",
        "http_url": "http://www.alphabet.com",
        "request_method": "POST",
        "params_list": [{
            "key": "testParam1",
            "parameter_type": "val",
            "value": "testValue1"
        }, {
            "key": "testParam2",
            "parameter_type": "slot",
            "value": "testValue1"
        }]
    }
    response = client.put(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_update_http_action_non_existing():
    request_body = {
        "auth_token": "",
        "action_name": "test_update_http_action_non_existing",
        "response": "",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": []
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    request_body = {
        "auth_token": "bearer hjklfsdjsjkfbjsbfjsvhfjksvfjksvfjksvf",
        "action_name": "test_update_http_action_non_existing_new",
        "response": "json",
        "http_url": "http://www.alphabet.com",
        "request_method": "POST",
        "params_list": [{
            "key": "param1",
            "value": "value1",
            "parameter_type": "value"},
            {
                "key": "param2",
                "value": "value2",
                "parameter_type": "slot"}]
    }
    response = client.put(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_delete_http_action():
    request_body = {
        "auth_token": "",
        "action_name": "test_delete_http_action",
        "response": "",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": []
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    response = client.delete(
        url=f"/api/bot/{pytest.bot}/action/httpaction/test_delete_http_action",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["message"]
    assert actual["success"]


def test_delete_http_action_non_existing():
    request_body = {
        "auth_token": "",
        "action_name": "new_http_action4",
        "response": "",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": []
    }

    client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    response = client.delete(
        url=f"/api/bot/{pytest.bot}/action/httpaction/new_http_action_never_added",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert actual["message"]
    assert not actual["success"]


def test_list_actions():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path_action",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "action_greet", "type": "ACTION"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow added successfully"
    assert actual['data']["_id"]
    assert actual["success"]

    response = client.get(
        url=f"/api/bot/{pytest.bot}/actions",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])
    assert actual['data'] == ["action_greet"]
    assert actual["success"]


@responses.activate
def test_train_using_event(monkeypatch):
    responses.add(
        responses.POST,
        "http://localhost/train",
        status=200
    )
    monkeypatch.setitem(Utility.environment['model']['train'], "event_url", "http://localhost/train")
    response = client.post(
        f"/api/bot/{pytest.bot}/train",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Model training started."


def test_update_training_data_generator_status(monkeypatch):
    request_body = {
        "status": EVENT_STATUS.INITIATED
    }
    response = client.put(
        f"/api/bot/{pytest.bot}/update/data/generator/status",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Status updated successfully!"


def test_get_training_data_history(monkeypatch):
    response = client.get(
        f"/api/bot/{pytest.bot}/data/generation/history",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    response = actual["data"]
    assert response is not None
    response['status'] = 'Initiated'
    assert actual["message"] is None


def test_update_training_data_generator_status_completed(monkeypatch):
    training_data = [{
        "intent": "intent1_test_add_training_data",
        "training_examples": ["example1", "example2"],
        "response": "response1"},
        {"intent": "intent2_test_add_training_data",
         "training_examples": ["example3", "example4"],
         "response": "response2"}]
    request_body = {
        "status": EVENT_STATUS.COMPLETED,
        "response": training_data
    }
    response = client.put(
        f"/api/bot/{pytest.bot}/update/data/generator/status",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Status updated successfully!"


def test_update_training_data_generator_wrong_status(monkeypatch):
    training_data = [{
        "intent": "intent1_test_add_training_data",
        "training_examples": ["example1", "example2"],
        "response": "response1"},
        {"intent": "intent2_test_add_training_data",
         "training_examples": ["example3", "example4"],
         "response": "response2"}]
    request_body = {
        "status": "test",
        "response": training_data
    }
    response = client.put(
        f"/api/bot/{pytest.bot}/update/data/generator/status",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual['data'] is None
    assert actual['error_code'] == 422
    assert str(actual['message']).__contains__("value is not a valid enumeration member")
    assert not actual['success']


def test_add_training_data(monkeypatch):
    response = client.get(
        f"/api/bot/{pytest.bot}/data/generation/history",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    response = actual["data"]
    assert response is not None
    response['status'] = 'Initiated'
    assert actual["message"] is None
    doc_id = response['training_history'][0]['_id']
    training_data = {
        "history_id": doc_id,
        "training_data": [{
            "intent": "intent1_test_add_training_data",
            "training_examples": ["example1", "example2"],
            "response": "response1"},
            {"intent": "intent2_test_add_training_data",
             "training_examples": ["example3", "example4"],
             "response": "response2"}]}
    response = client.post(
        f"/api/bot/{pytest.bot}/data/bulk",
        json=training_data,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is not None
    assert actual["message"] == "Training data added successfully!"

    assert Intents.objects(name="intent1_test_add_training_data").get() is not None
    assert Intents.objects(name="intent2_test_add_training_data").get() is not None
    training_examples = list(TrainingExamples.objects(intent="intent1_test_add_training_data"))
    assert training_examples is not None
    assert len(training_examples) == 2
    training_examples = list(TrainingExamples.objects(intent="intent2_test_add_training_data"))
    assert len(training_examples) == 2
    assert Responses.objects(name="utter_intent1_test_add_training_data") is not None
    assert Responses.objects(name="utter_intent2_test_add_training_data") is not None
    story = Stories.objects(block_name="path_intent1_test_add_training_data").get()
    assert story is not None
    assert story['events'][0]['name'] == 'intent1_test_add_training_data'
    assert story['events'][0]['type'] == StoryEventType.user
    assert story['events'][1]['name'] == "utter_intent1_test_add_training_data"
    assert story['events'][1]['type'] == StoryEventType.action
    story = Stories.objects(block_name="path_intent2_test_add_training_data").get()
    assert story is not None
    assert story['events'][0]['name'] == 'intent2_test_add_training_data'
    assert story['events'][0]['type'] == StoryEventType.user
    assert story['events'][1]['name'] == "utter_intent2_test_add_training_data"
    assert story['events'][1]['type'] == StoryEventType.action


def test_get_training_data_history_1(monkeypatch):
    response = client.get(
        f"/api/bot/{pytest.bot}/data/generation/history",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] is None
    training_data = actual["data"]['training_history'][0]

    assert training_data['status'] == EVENT_STATUS.COMPLETED.value
    end_timestamp = training_data['end_timestamp']
    assert end_timestamp is not None
    assert training_data['last_update_timestamp'] == end_timestamp
    response = training_data['response']
    assert response is not None
    assert response[0]['intent'] == 'intent1_test_add_training_data'
    assert response[0]['training_examples'][0]['training_example'] == "example1"
    assert response[0]['training_examples'][0]['is_persisted']
    assert response[0]['training_examples'][1]['training_example'] == "example2"
    assert response[0]['training_examples'][1]['is_persisted']
    assert response[0]['response'] == 'response1'
    assert response[1]['intent'] == 'intent2_test_add_training_data'
    assert response[1]['training_examples'][0]['training_example'] == "example3"
    assert response[1]['training_examples'][0]['is_persisted']
    assert response[1]['training_examples'][1]['training_example'] == "example4"
    assert response[1]['training_examples'][1]['is_persisted']
    assert response[1]['response'] == 'response2'


def test_update_training_data_generator_status_exception(monkeypatch):
    request_body = {
        "status": EVENT_STATUS.INITIATED,
    }
    response = client.put(
        f"/api/bot/{pytest.bot}/update/data/generator/status",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Status updated successfully!"

    request_body = {
        "status": EVENT_STATUS.FAIL,
        "exception": 'Exception message'
    }
    response = client.put(
        f"/api/bot/{pytest.bot}/update/data/generator/status",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["message"] == "Status updated successfully!"


def test_get_training_data_history_2(monkeypatch):
    response = client.get(
        f"/api/bot/{pytest.bot}/data/generation/history",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] is None
    training_data = actual["data"]['training_history'][0]
    assert training_data['status'] == EVENT_STATUS.FAIL.value
    end_timestamp = training_data['end_timestamp']
    assert end_timestamp is not None
    assert training_data['last_update_timestamp'] == end_timestamp
    assert training_data['exception'] == 'Exception message'


def test_fetch_latest(monkeypatch):
    request_body = {
        "status": EVENT_STATUS.INITIATED,
    }
    response = client.put(
        f"/api/bot/{pytest.bot}/update/data/generator/status",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    response = client.get(
        f"/api/bot/{pytest.bot}/data/generation/latest",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]['status'] == EVENT_STATUS.INITIATED.value
    assert actual["message"] is None


async def mock_upload(doc):
    if not (doc.filename.lower().endswith('.pdf') or doc.filename.lower().endswith('.docx')):
        raise AppException("Invalid File Format")


@pytest.fixture
def mock_file_upload(monkeypatch):
    def _in_progress_mock(*args, **kwargs):
        return None

    def _daily_limit_mock(*args, **kwargs):
        return None

    def _set_status_mock(*args, **kwargs):
        return None

    def _train_data_gen(*args, **kwargs):
        return None

    monkeypatch.setattr(TrainingDataGenerationProcessor, "is_in_progress", _in_progress_mock)
    monkeypatch.setattr(TrainingDataGenerationProcessor, "check_data_generation_limit", _daily_limit_mock)
    monkeypatch.setattr(TrainingDataGenerationProcessor, "set_status", _set_status_mock)
    monkeypatch.setattr(DataUtility, "trigger_data_generation_event", _train_data_gen)


def test_file_upload_docx(mock_file_upload, monkeypatch):
    monkeypatch.setattr(Utility, "upload_document", mock_upload)

    response = client.post(
        f"/api/bot/{pytest.bot}/upload/data_generation/file",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files={"doc": (
            "tests/testing_data/file_data/sample1.docx",
            open("tests/testing_data/file_data/sample1.docx", "rb"))})

    actual = response.json()
    assert actual["message"] == "File uploaded successfully and training data generation has begun"
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]


def test_file_upload_pdf(mock_file_upload, monkeypatch):
    monkeypatch.setattr(Utility, "upload_document", mock_upload)

    response = client.post(
        f"/api/bot/{pytest.bot}/upload/data_generation/file",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files={"doc": (
            "tests/testing_data/file_data/sample1.pdf",
            open("tests/testing_data/file_data/sample1.pdf", "rb"))})

    actual = response.json()
    assert actual["message"] == "File uploaded successfully and training data generation has begun"
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]


def test_file_upload_error(mock_file_upload, monkeypatch):
    monkeypatch.setattr(Utility, "upload_document", mock_upload)

    response = client.post(
        f"/api/bot/{pytest.bot}/upload/data_generation/file",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files={"doc": (
            "nlu.md",
            open("tests/testing_data/all/data/nlu.md", "rb"))})

    actual = response.json()
    assert actual["message"] == "Invalid File Format"
    assert actual["error_code"] == 422
    assert not actual["success"]


def test_list_action_server_logs_empty():
    response = client.get(
        f"/api/bot/{pytest.bot}/actions/logs",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token})

    actual = response.json()
    assert actual['data']['logs'] == []
    assert actual['data']['total'] == 0


def test_list_action_server_logs():
    bot = pytest.bot
    bot_2 = "integration2"
    request_params = {"key": "value", "key2": "value2"}
    expected_intents = ["intent13", "intent11", "intent9", "intent8", "intent7", "intent6", "intent5",
                        "intent4", "intent3", "intent2"]
    ActionServerLogs(intent="intent1", action="http_action", sender="sender_id", timestamp='2021-04-05T07:59:08.771000',
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot).save()
    ActionServerLogs(intent="intent2", action="http_action", sender="sender_id",
                     url="http://kairon-api.digite.com/api/bot",
                     request_params=request_params, api_response="Response", bot_response="Bot Response", bot=bot,
                     status="FAILURE").save()
    ActionServerLogs(intent="intent1", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot_2).save()
    ActionServerLogs(intent="intent3", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response", bot=bot,
                     status="FAILURE").save()
    ActionServerLogs(intent="intent4", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot).save()
    ActionServerLogs(intent="intent5", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response", bot=bot,
                     status="FAILURE").save()
    ActionServerLogs(intent="intent6", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot).save()
    ActionServerLogs(intent="intent7", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot).save()
    ActionServerLogs(intent="intent8", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot).save()
    ActionServerLogs(intent="intent9", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot).save()
    ActionServerLogs(intent="intent10", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot_2).save()
    ActionServerLogs(intent="intent11", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response",
                     bot=bot).save()
    ActionServerLogs(intent="intent12", action="http_action", sender="sender_id",
                     request_params=request_params, api_response="Response", bot_response="Bot Response", bot=bot_2,
                     status="FAILURE").save()
    ActionServerLogs(intent="intent13", action="http_action", sender="sender_id_13",
                     request_params=request_params, api_response="Response", bot_response="Bot Response", bot=bot,
                     status="FAILURE").save()
    response = client.get(
        f"/api/bot/{pytest.bot}/actions/logs",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token})

    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["success"]
    assert len(actual['data']['logs']) == 10
    assert actual['data']['total'] == 11
    assert [log['intent'] in expected_intents for log in actual['data']['logs']]
    assert actual['data']['logs'][0]['action'] == "http_action"
    assert any([log['request_params'] == request_params for log in actual['data']['logs']])
    assert any([log['sender'] == "sender_id_13" for log in actual['data']['logs']])
    assert any([log['bot_response'] == "Bot Response" for log in actual['data']['logs']])
    assert any([log['api_response'] == "Response" for log in actual['data']['logs']])
    assert any([log['status'] == "FAILURE" for log in actual['data']['logs']])
    assert any([log['status'] == "SUCCESS" for log in actual['data']['logs']])

    response = client.get(
        f"/api/bot/{pytest.bot}/actions/logs?start_idx=0&page_size=15",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert len(actual['data']['logs']) == 11
    assert actual['data']['total'] == 11

    response = client.get(
        f"/api/bot/{pytest.bot}/actions/logs?start_idx=10&page_size=1",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["success"]
    assert len(actual['data']['logs']) == 1
    assert actual['data']['total'] == 11


def test_add_training_data_invalid_id(monkeypatch):
    request_body = {
        "status": EVENT_STATUS.INITIATED
    }
    client.put(
        f"/api/bot/{pytest.bot}/update/data/generator/status",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    response = client.get(
        f"/api/bot/{pytest.bot}/data/generation/history",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    response = actual["data"]
    assert response is not None
    response['status'] = 'Initiated'
    assert actual["message"] is None
    doc_id = response['training_history'][0]['_id']
    training_data = {
        "history_id": doc_id,
        "training_data": [{
            "intent": "intent1_test_add_training_data",
            "training_examples": ["example1", "example2"],
            "response": "response1"},
            {"intent": "intent2_test_add_training_data",
             "training_examples": ["example3", "example4"],
             "response": "response2"}]}
    response = client.post(
        f"/api/bot/{pytest.bot}/data/bulk",
        json=training_data,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["data"] is None
    assert actual["message"] == "No Training Data Generated"


def test_feedback():
    request = {
        'rating': 5.0, 'scale': 5.0, 'feedback': 'The product is better than rasa.'
    }
    response = client.post(
        f"/api/account/feedback",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        json=request
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert not actual["data"]
    assert actual["message"] == 'Thanks for your feedback!'


def test_add_rule():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow added successfully"
    assert actual["data"]["_id"]


def test_add_rule_invalid_type():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "TEST",
            "template_type": "Q&A",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [{'ctx': {'enum_values': ['STORY', 'RULE']}, 'loc': ['body', 'type'],
                                  'msg': "value is not a valid enumeration member; permitted: 'STORY', 'RULE'",
                                  'type': 'type_error.enum'}]


def test_add_rule_empty_event():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={"name": "test_add_rule_empty_event", "type": "RULE", "steps": []},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'steps'], 'msg': 'Steps are required to form Flow', 'type': 'value_error'}]


def test_add_rule_lone_intent():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_rule_lone_intent",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
                {"name": "greet_again", "type": "INTENT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'steps'], 'msg': 'Intent should be followed by utterance or action', 'type': 'value_error'}]


def test_add_rule_consecutive_intents():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_rule_consecutive_intents",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'steps'], 'msg': 'Found 2 consecutive intents', 'type': 'value_error'}]


def test_add_rule_multiple_actions():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_rule_consecutive_actions",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "HTTP_ACTION"},
                {"name": "utter_greet_again", "type": "HTTP_ACTION"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow added successfully"


def test_add_rule_utterance_as_first_step():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_rule_consecutive_intents",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "BOT"},
                {"name": "utter_greet", "type": "HTTP_ACTION"},
                {"name": "utter_greet_again", "type": "HTTP_ACTION"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'steps'], 'msg': 'First step should be an intent', 'type': 'value_error'}]


def test_add_rule_missing_event_type():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "RULE",
            "steps": [{"name": "greet"}, {"name": "utter_greet", "type": "BOT"}],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert (
            actual["message"]
            == [{'loc': ['body', 'steps', 0, 'type'], 'msg': 'field required', 'type': 'value_error.missing'}]
    )


def test_add_rule_invalid_event_type():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "data"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert (
            actual["message"]
            == [{'ctx': {'enum_values': ['INTENT', 'BOT', 'HTTP_ACTION', 'ACTION', 'SLOT_SET_ACTION']},
                 'loc': ['body', 'steps', 0, 'type'],
                 'msg': "value is not a valid enumeration member; permitted: 'INTENT', 'BOT', 'HTTP_ACTION', 'ACTION', 'SLOT_SET_ACTION'",
                 'type': 'type_error.enum'}]
    )


def test_update_rule():
    response = client.put(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_nonsense", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow updated successfully"
    assert actual["data"]["_id"]


def test_update_rule_invalid_event_type():
    response = client.put(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "data"},
                {"name": "utter_nonsense", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert (
            actual["message"]
            == [{'ctx': {'enum_values': ['INTENT', 'BOT', 'HTTP_ACTION', 'ACTION', 'SLOT_SET_ACTION']},
                 'loc': ['body', 'steps', 0, 'type'],
                 'msg': "value is not a valid enumeration member; permitted: 'INTENT', 'BOT', 'HTTP_ACTION', 'ACTION', 'SLOT_SET_ACTION'",
                 'type': 'type_error.enum'}]
    )


def test_delete_rule():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path1",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow added successfully"

    response = client.delete(
        f"/api/bot/{pytest.bot}/stories/test_path1/RULE",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow deleted successfully"


def test_delete_non_existing_rule():
    response = client.delete(
        f"/api/bot/{pytest.bot}/stories/test_path2/RULE",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Flow does not exists"


def test_add_rule_with_multiple_intents():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "RULE",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
                {"name": "location", "type": "INTENT"},
                {"name": "utter_location", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [{'loc': ['body', 'steps'],
                                  'msg': "Found rules 'test_path' that contain more than intent.\nPlease use stories for this case",
                                  'type': 'value_error'}]
    assert actual["data"] is None


def test_validate():
    response = client.post(
        f"/api/bot/{pytest.bot}/validate",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert not actual["data"]
    assert actual["message"] == 'Event triggered! Check logs.'


def test_upload_missing_data():
    files = (('training_files', ("domain.yml", BytesIO(open("tests/testing_data/all/domain.yml", "rb").read()))),
             ('training_files', ("stories.md", BytesIO(open("tests/testing_data/all/data/stories.md", "rb").read()))),
             ('training_files', ("config.yml", BytesIO(open("tests/testing_data/all/config.yml", "rb").read()))),
             )
    response = client.post(
        f"/api/bot/{pytest.bot}/upload",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=files,
    )
    actual = response.json()
    assert actual["message"] == 'Upload in progress! Check logs.'
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]


def test_upload_valid_and_invalid_data():
    files = (('training_files', ("nlu_1.md", None)),
             ('training_files', ("domain_5.yml", open("tests/testing_data/all/domain.yml", "rb"))),
             ('training_files', ("stories.md", open("tests/testing_data/all/data/stories.md", "rb"))),
             ('training_files', ("config_6.yml", open("tests/testing_data/all/config.yml", "rb"))))
    response = client.post(
        f"/api/bot/{pytest.bot}/upload",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=files,
    )
    actual = response.json()
    assert actual["message"] == 'Upload in progress! Check logs.'
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]


def test_upload_with_http_error():
    config = Utility.load_yaml("./tests/testing_data/yml_training_files/config.yml")
    config.get('pipeline').append({'name': "XYZ"})
    files = (('training_files', ("config.yml", json.dumps(config).encode())),
             ('training_files', ("http_action.yml", open("tests/testing_data/error/http_action.yml", "rb"))))

    response = client.post(
        f"/api/bot/{pytest.bot}/upload",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=files,
    )
    actual = response.json()
    assert actual["message"] == "Upload in progress! Check logs."
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]

    response = client.get(
        f"/api/bot/{pytest.bot}/importer/logs",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual["data"]) == 4
    assert actual['data'][0]['status'] == 'Failure'
    assert actual['data'][0]['event_status'] == EVENT_STATUS.COMPLETED.value
    assert actual['data'][0]['is_data_uploaded']
    assert actual['data'][0]['start_timestamp']
    assert actual['data'][0]['start_timestamp']
    assert actual['data'][0]['start_timestamp']
    assert actual['data'][0]['http_actions']['data'] == ['Required http action fields not found']
    assert actual['data'][0]['config']['data'] == ['Invalid component XYZ']


def test_upload_actions_and_config():
    files = (('training_files', ("config.yml", open("tests/testing_data/yml_training_files/config.yml", "rb"))),
             ('training_files',
              ("http_action.yml", open("tests/testing_data/yml_training_files/http_action.yml", "rb"))))

    response = client.post(
        f"/api/bot/{pytest.bot}/upload",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
        files=files,
    )
    actual = response.json()
    assert actual["message"] == "Upload in progress! Check logs."
    assert actual["error_code"] == 0
    assert actual["data"] is None
    assert actual["success"]

    response = client.get(
        f"/api/bot/{pytest.bot}/importer/logs",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual["data"]) == 5
    assert actual['data'][0]['status'] == 'Success'
    assert actual['data'][0]['event_status'] == EVENT_STATUS.COMPLETED.value
    assert actual['data'][0]['is_data_uploaded']
    assert actual['data'][0]['start_timestamp']
    assert actual['data'][0]['start_timestamp']
    assert actual['data'][0]['start_timestamp']
    assert actual['data'][0]['http_actions']['count'] == 5
    assert not actual['data'][0]['http_actions']['data']
    assert not actual['data'][0]['config']['data']

    response = client.get(
        f"/api/bot/{pytest.bot}/action/httpaction",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual["data"]) == 5


def test_get_editable_config():
    response = client.get(f"/api/bot/{pytest.bot}/config/properties",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual['data'] == {'nlu_confidence_threshold': 70, 'action_fallback': 'action_default_fallback',
                              'action_fallback_threshold': 30,
                              'ted_epochs': 5, 'nlu_epochs': 5, 'response_epochs': 5}


def test_set_epoch_and_fallback():
    request = {"nlu_epochs": 200,
               "response_epochs": 300,
               "ted_epochs": 400,
               "nlu_confidence_threshold": 70,
               'action_fallback_threshold': 30,
               "action_fallback": "action_default_fallback"}
    response = client.post(
        f"/api/bot/{pytest.bot}/response/utter_default",
        json={"data": "Sorry I didnt get that. Can you rephrase?"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Response added!"

    response = client.put(f"/api/bot/{pytest.bot}/config/properties",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token},
                          json=request)
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == 'Config saved'


def test_get_config_all():
    response = client.get(f"/api/bot/{pytest.bot}/config/properties",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual['data']


def test_set_epoch_and_fallback_modify_action_only():
    request = {"nlu_confidence_threshold": 30,
               "action_fallback": "utter_default"}
    response = client.put(f"/api/bot/{pytest.bot}/config/properties",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token},
                          json=request)
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == 'Config saved'


def test_set_epoch_and_fallback_empty_pipeline_and_policies():
    request = {"nlu_confidence_threshold": 20}
    response = client.put(f"/api/bot/{pytest.bot}/config/properties",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token},
                          json=request)
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'nlu_confidence_threshold'], 'msg': 'Please choose a threshold between 30 and 90',
         'type': 'value_error'}]


def test_set_epoch_and_fallback_empty_request():
    response = client.put(f"/api/bot/{pytest.bot}/config/properties",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token},
                          json={})
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'At least one field is required'


def test_set_epoch_and_fallback_negative_epochs():
    response = client.put(f"/api/bot/{pytest.bot}/config/properties",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token},
                          json={'nlu_epochs': 0})
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == [
        {'loc': ['body', 'nlu_epochs'], 'msg': 'Choose a positive number as epochs', 'type': 'value_error'}]

    response = client.put(f"/api/bot/{pytest.bot}/config/properties",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token},
                          json={'response_epochs': -1, 'ted_epochs': 0, 'nlu_epochs': 200})
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0] == {'loc': ['body', 'response_epochs'], 'msg': 'Choose a positive number as epochs',
                                    'type': 'value_error'}
    assert actual["message"][1] == {'loc': ['body', 'ted_epochs'], 'msg': 'Choose a positive number as epochs',
                                    'type': 'value_error'}


def test_get_synonyms():
    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 0
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_add_synonyms():
    response = client.post(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        json={"name": "bot_add", "value": ["any"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Synonym and values added successfully!"

    client.post(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        json={"name": "bot_add", "value": ["any1"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual['data'] == [{"any": "bot_add"}, {"any1": "bot_add"}]


def test_get_specific_synonym_values():
    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms/bot_add",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert len(actual['data']) == 2


def test_add_synonyms_duplicate():
    response = client.post(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        json={"name": "bot_add", "value": ["any"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Synonym value already exists"


def test_add_synonyms_value_empty():
    response = client.post(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        json={"name": "bot_add", "value": []},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0]['msg'] == "value field cannot be empty"


def test_add_synonyms_empty():
    response = client.post(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        json={"name": "", "value": ["h"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0]['msg'] == "synonym cannot be empty"


def test_edit_synonyms():
    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms/bot_add",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    response = client.put(
        f"/api/bot/{pytest.bot}/entity/synonyms/bot_add/{actual['data'][0]['_id']}",
        json={"data": "any4"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Synonym updated!"

    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert len(actual['data']) == 2
    value_list = [list(actual['data'][0].keys())[0], list(actual['data'][1].keys())[0]]
    assert "any4" in value_list


def test_delete_synonym_one_value():
    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms/bot_add",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    response = client.delete(
        f"/api/bot/{pytest.bot}/entity/synonyms/False",
        json={"data": actual['data'][0]['_id']},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Synonym removed!"

    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert len(actual['data']) == 1


def test_delete_synonym():
    response = client.delete(
        f"/api/bot/{pytest.bot}/entity/synonyms/True",
        json={"data": "bot_add"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Synonym removed!"

    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual['data'] == []


def test_add_synonyms_empty_value_element():
    response = client.post(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        json={"name": "bot_add", "value": ['df', '']},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0]['msg'] == "value cannot be an empty string"


def test_get_training_data_count(monkeypatch):
    def _mock_training_data_count(*args, **kwargs):
        return {
            'intents': [{'name': 'greet', 'count': 5}, {'name': 'affirm', 'count': 3}],
            'utterances': [{'name': 'utter_greet', 'count': 4}, {'name': 'utter_affirm', 'count': 11}]
        }

    monkeypatch.setattr(MongoProcessor, 'get_training_data_count', _mock_training_data_count)
    response = client.get(f"/api/bot/{pytest.bot}/data/count",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] == _mock_training_data_count()


@responses.activate
def test_chat(monkeypatch):
    monkeypatch.setitem(Utility.environment['model']['agent'], 'url', "http://localhost")
    chat_json = {"data": "Hi"}
    responses.add(
        responses.POST,
        f"http://localhost/api/bot/{pytest.bot}/chat",
        status=200,
        match=[
            responses.json_params_matcher(
                chat_json)],
        json={'success': True, 'error_code': 0, "data": {'response': [{'bot': 'Hi'}]}, 'message': None}
    )
    response = client.post(f"/api/bot/{pytest.bot}/chat",
                           json=chat_json,
                           headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]['response']


@responses.activate
def test_chat_user(monkeypatch):
    monkeypatch.setitem(Utility.environment['model']['agent'], 'url', "http://localhost")
    chat_json = {"data": "Hi"}
    responses.add(
        responses.POST,
        f"http://localhost/api/bot/{pytest.bot}/chat",
        status=200,
        match=[
            responses.json_params_matcher(
                chat_json)],
        json={'success': True, 'error_code': 0, "data": {'response': [{'bot': 'Hi'}]}, 'message': None}
    )
    response = client.post(f"/api/bot/{pytest.bot}/chat",
                           json=chat_json,
                           headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]['response']


@responses.activate
def test_chat_augment_user(monkeypatch):
    monkeypatch.setitem(Utility.environment['model']['agent'], 'url', "http://localhost")
    chat_json = {"data": "Hi"}
    responses.add(
        responses.POST,
        f"http://localhost/api/bot/{pytest.bot}/chat",
        status=200,
        match=[
            responses.json_params_matcher(
                chat_json)],
        json={'success': True, 'error_code': 0, "data": {'response': [{'bot': 'Hi'}]}, 'message': None}
    )
    response = client.post(f"/api/bot/{pytest.bot}/chat/testUser",
                           json=chat_json,
                           headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]['response']


def test_get_client_config():
    response = client.get(f"/api/bot/{pytest.bot}/chat/client/config",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]


def test_get_client_config_url():
    response = client.get(f"/api/bot/{pytest.bot}/chat/client/config/url",
                          headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]
    pytest.url = actual["data"]


def test_get_client_config_using_invalid_uid():
    response = client.get(f'/api/bot/{pytest.bot}/chat/client/config/ecmkfnufjsufysfbksjnfaksn')
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert not actual["data"]


def test_save_client_config():
    config_path = "./template/chat-client/default-config.json"
    config = json.load(open(config_path))
    config['headers'] = {}
    config['headers']['X-USER'] = 'kairon-user'
    response = client.post(f"/api/bot/{pytest.bot}/chat/client/config",
                           json={'data': config},
                           headers={"Authorization": pytest.token_type + " " + pytest.access_token})
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == 'Config saved'

    config = ChatClientConfig.objects(bot=pytest.bot).get()
    assert config.config
    assert config.config['headers']['X-USER']
    assert not config.config['headers'].get('authorization')


@responses.activate
def test_get_client_config_using_uid(monkeypatch):
    monkeypatch.setitem(Utility.environment['model']['agent'], 'url', "http://localhost")
    chat_json = {"data": "Hi"}
    responses.add(
        responses.POST,
        f"http://localhost/api/bot/{pytest.bot}/chat",
        status=200,
        match=[
            responses.json_params_matcher(
                chat_json)],
        json={'success': True, 'error_code': 0, "data": None, 'message': "Bot has not been trained yet!"}
    )
    response = client.get(pytest.url)
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]

    auth_token = actual['data']['headers']['authorization']
    response = client.post(
        f"/api/bot/{pytest.bot}/chat",
        json=chat_json,
        headers={
            "Authorization": auth_token, 'X-USER': 'hacker'
        },
    )
    actual = response.json()
    assert actual["message"] == "Bot has not been trained yet!"

    response = client.get(
        f"/api/bot/{pytest.bot}/intents",
        headers={
            "Authorization": auth_token, 'X-USER': 'hacker'
        },
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert not actual["success"]
    assert actual["message"] == 'Access denied for this endpoint'


@responses.activate
def test_get_client_config_refresh(monkeypatch):
    monkeypatch.setitem(Utility.environment['model']['agent'], 'url', "http://localhost")
    chat_json = {"data": "Hi"}
    responses.add(
        responses.POST,
        f"http://localhost/api/bot/{pytest.bot}/chat",
        status=200,
        match=[
            responses.json_params_matcher(
                chat_json)],
        json={'success': True, 'error_code': 0, "data": None, 'message': "Bot has not been trained yet!"}
    )
    response = client.get(pytest.url)
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]
    assert actual['data']['headers']['X-USER'] == 'kairon-user'

    auth_token = actual['data']['headers']['authorization']
    user = actual['data']['headers']['X-USER']
    response = client.post(
        f"/api/bot/{pytest.bot}/chat",
        json=chat_json,
        headers={
            "Authorization": auth_token, 'X-USER': user
        },
    )
    actual = response.json()
    assert actual["message"] == "Bot has not been trained yet!"

    response = client.get(
        f"/api/bot/{pytest.bot}/intents",
        headers={
            "Authorization": auth_token, 'X-USER': user
        },
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert not actual["success"]
    assert actual["message"] == 'Access denied for this endpoint'


def test_add_story_with_no_type():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_add_story_with_no_type",
            "type": "STORY",
            "steps": [
                {"name": "greet", "type": "INTENT"},
                {"name": "utter_greet", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow added successfully"

    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "test_path",
            "type": "STORY",
            "steps": [
                {"name": "test_greet", "type": "INTENT"},
                {"name": "utter_test_greet", "type": "ACTION"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["message"] == "Flow added successfully"
    assert actual["success"]
    assert actual["error_code"] == 0


def test_get_stories_another_bot():
    response = client.get(
        f"/api/bot/{pytest.bot}/stories",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]
    assert actual["data"][0]['template_type'] == 'CUSTOM'
    assert actual["data"][1]['template_type'] == 'CUSTOM'
    assert actual["data"][8]['template_type'] == 'Q&A'
    assert actual["data"][8]['name'] == 'test_add_story_with_no_type'
    assert actual["data"][9]['template_type'] == 'CUSTOM'
    assert actual["data"][9]['name'] == 'test_path'


def test_add_regex_invalid():
    response = client.post(
        f"/api/bot/{pytest.bot}/regex",
        json={"name": "bot_add", "pattern": "[0-9]++"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'invalid regular expression'


def test_add_regex_empty_name():
    response = client.post(
        f"/api/bot/{pytest.bot}/regex",
        json={"name": "", "pattern": "q"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0]['msg'] == 'Regex name cannot be empty or a blank space'


def test_add_regex_empty_pattern():
    response = client.post(
        f"/api/bot/{pytest.bot}/regex",
        json={"name": "b", "pattern": ""},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0]['msg'] == 'Regex pattern cannot be empty or a blank space'


def test_add_regex_():
    response = client.post(
        f"/api/bot/{pytest.bot}/regex",
        json={"name": "b", "pattern": "bb"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Regex pattern added successfully!"


def test_get_regex():
    response = client.get(
        f"/api/bot/{pytest.bot}/regex",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 1
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])
    assert "b" in actual['data'][0].values()
    assert "bb" in actual['data'][0].values()


def test_edit_regex():
    response = client.put(
        f"/api/bot/{pytest.bot}/regex",
        json={"name": "b", "pattern": "bbb"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == 'Regex pattern modified successfully!'

    response = client.get(
        f"/api/bot/{pytest.bot}/regex",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 1
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])
    assert "b" in actual['data'][0].values()
    assert "bbb" in actual['data'][0].values()


def test_delete_regex():
    response = client.delete(
        f"/api/bot/{pytest.bot}/regex/b",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == 'Regex pattern deleted!'

    response = client.get(
        f"/api/bot/{pytest.bot}/regex",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 0
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_add_and_move_training_examples_to_different_intent():
    response = client.post(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        json={"data": ["hey, there [bot](bot)!!"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"][0]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] is None
    response = client.get(
        f"/api/bot/{pytest.bot}/training_examples/greet",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 7

    response = client.post(
        f"/api/bot/{pytest.bot}/intents",
        json={"data": "test_add_and_move"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Intent added successfully!"

    response = client.post(
        f"/api/bot/{pytest.bot}/training_examples/move/test_add_and_move",
        json={"data": ["this will be moved", "this is a new [example](example)", " ", "", "hey, there [bot](bot)!!"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"][0]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] is None
    response = client.get(
        f"/api/bot/{pytest.bot}/training_examples/test_add_and_move",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual["data"]) == 3


def test_add_and_move_training_examples_to_different_intent_not_exists():
    response = client.post(
        f"/api/bot/{pytest.bot}/training_examples/move/greeting",
        json={"data": ["this will be moved", "this is a new [example](example)", " ", "", "hey, there [bot](bot)!!"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Intent does not exists'


def test_get_lookup_tables():
    response = client.get(
        f"/api/bot/{pytest.bot}/lookup/tables",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"]) == 0
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])


def test_add_lookup_tables():
    response = client.post(
        f"/api/bot/{pytest.bot}/lookup/tables",
        json={"name": "country", "value": ["india", "australia"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Lookup table and values added successfully!"

    client.post(
        f"/api/bot/{pytest.bot}/lookup/tables",
        json={"name": "number", "value": ["one", "two"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    response = client.get(
        f"/api/bot/{pytest.bot}/lookup/tables",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual['data'] == [{'name': 'country', 'elements': ['india', 'australia']},
                              {'name': 'number', 'elements': ['one', 'two']}]


def test_get_lookup_table_values():
    response = client.get(
        f"/api/bot/{pytest.bot}/lookup/tables/country",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert len(actual['data']) == 2


def test_add_lookup_duplicate():
    response = client.post(
        f"/api/bot/{pytest.bot}/lookup/tables",
        json={"name": "country", "value": ["india"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == "Lookup table value already exists"


def test_add_lookup_empty():
    response = client.post(
        f"/api/bot/{pytest.bot}/lookup/tables",
        json={"name": "country", "value": []},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0]['msg'] == "value field cannot be empty"


def test_edit_lookup():
    response = client.get(
        f"/api/bot/{pytest.bot}/lookup/tables/country",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    response = client.put(
        f"/api/bot/{pytest.bot}/lookup/tables/country/{actual['data'][0]['_id']}",
        json={"data": "japan"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Lookup table updated!"


def test_add_lookup_empty_name():
    response = client.post(
        f"/api/bot/{pytest.bot}/lookup/tables",
        json={"name": "", "value": ["h"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0]['msg'] == "name cannot be empty or a blank space"


def test_delete_lookup_one_value():
    response = client.get(
        f"/api/bot/{pytest.bot}/lookup/tables/country",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    response = client.delete(
        f"/api/bot/{pytest.bot}/lookup/tables/False",
        json={"data": actual['data'][0]['_id']},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Lookup Table removed!"

    response = client.get(
        f"/api/bot/{pytest.bot}/lookup/tables",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert len(actual['data']) == 2


def test_delete_lookup():
    response = client.delete(
        f"/api/bot/{pytest.bot}/lookup/tables/True",
        json={"data": "country"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Lookup Table removed!"

    response = client.get(
        f"/api/bot/{pytest.bot}/lookup/tables",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert len(actual['data']) == 1


def test_add_lookup_empty_value_element():
    response = client.post(
        f"/api/bot/{pytest.bot}/lookup/tables",
        json={"name": "country", "value": ['df', '']},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"][0]['msg'] == "lookup value cannot be empty or a blank space"


def test_list_form_none_exists():
    response = client.get(
        f"/api/bot/{pytest.bot}/forms",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] == []


def test_add_form():
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "name", "type": "text"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "num_people", "type": "float"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "cuisine", "type": "text"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "outdoor_seating", "type": "text"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "preferences", "type": "text"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "feedback", "type": "text"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]

    path = [{'responses': ['please give us your name?'], 'slot': 'name',
             'mapping': [{'type': 'from_text', 'value': 'user', 'entity': 'name'},
                         {'type': 'from_entity', 'entity': 'name'}]},
            {'responses': ['seats required?'], 'slot': 'num_people',
             'mapping': [{'type': 'from_entity', 'intent': ['inform', 'request_restaurant'], 'entity': 'number'}]},
            {'responses': ['type of cuisine?'], 'slot': 'cuisine',
             'mapping': [{'type': 'from_entity', 'entity': 'cuisine'}]},
            {'responses': ['outdoor seating required?'], 'slot': 'outdoor_seating',
             'mapping': [{'type': 'from_entity', 'entity': 'seating'},
                         {'type': 'from_intent', 'intent': ['affirm'], 'value': True},
                         {'type': 'from_intent', 'intent': ['deny'], 'value': False}]},
            {'responses': ['any preferences?'], 'slot': 'preferences',
             'mapping': [{'type': 'from_text', 'not_intent': ['affirm']},
                         {'type': 'from_intent', 'intent': ['affirm'], 'value': 'no additional preferences'}]},
            {'responses': ['Please give your feedback on your experience so far'], 'slot': 'feedback',
             'mapping': [{'type': 'from_text'},
                         {'type': 'from_entity', 'entity': 'feedback'}]},
            ]
    request = {'name': 'restaurant_form', 'path': path}
    response = client.post(
        f"/api/bot/{pytest.bot}/forms",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Form added"


def test_get_form_with_no_validations():
    response = client.get(
        f"/api/bot/{pytest.bot}/forms/restaurant_form",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    form = actual["data"]
    assert len(form['mapping']) == 6
    assert form['slot_mapping'][0]['slot'] == 'name'
    assert form['slot_mapping'][1]['slot'] == 'num_people'
    assert form['slot_mapping'][2]['slot'] == 'cuisine'
    assert form['slot_mapping'][3]['slot'] == 'outdoor_seating'
    assert form['slot_mapping'][4]['slot'] == 'preferences'
    assert form['slot_mapping'][5]['slot'] == 'feedback'
    assert form['slot_mapping'][0]['utterance'][0]['_id']
    assert form['slot_mapping'][1]['utterance'][0]['_id']
    assert form['slot_mapping'][2]['utterance'][0]['_id']
    assert form['slot_mapping'][3]['utterance'][0]['_id']
    assert form['slot_mapping'][4]['utterance'][0]['_id']
    assert form['slot_mapping'][5]['utterance'][0]['_id']
    assert form['slot_mapping'][0]['utterance'][0]['value']['text'] == 'please give us your name?'
    assert form['slot_mapping'][1]['utterance'][0]['value']['text'] == 'seats required?'
    assert form['slot_mapping'][2]['utterance'][0]['value']['text'] == 'type of cuisine?'
    assert form['slot_mapping'][3]['utterance'][0]['value']['text'] == 'outdoor seating required?'
    assert form['slot_mapping'][4]['utterance'][0]['value']['text'] == 'any preferences?'
    assert form['slot_mapping'][5]['utterance'][0]['value'][
               'text'] == 'Please give your feedback on your experience so far'
    assert form['slot_mapping'][0]['mapping'] == [{'type': 'from_text', 'value': 'user'},
                                                  {'type': 'from_entity', 'entity': 'name'}]
    assert form['slot_mapping'][1]['mapping'] == [
        {'type': 'from_entity', 'intent': ['inform', 'request_restaurant'], 'entity': 'number'}]
    assert form['slot_mapping'][2]['mapping'] == [{'type': 'from_entity', 'entity': 'cuisine'}]
    assert form['slot_mapping'][3]['mapping'] == [{'type': 'from_entity', 'entity': 'seating'},
                                                  {'type': 'from_intent', 'intent': ['affirm'], 'value': True},
                                                  {'type': 'from_intent', 'intent': ['deny'], 'value': False}]
    assert form['slot_mapping'][4]['mapping'] == [{'type': 'from_text', 'not_intent': ['affirm']},
                                                  {'type': 'from_intent', 'intent': ['affirm'],
                                                   'value': 'no additional preferences'}]
    assert form['slot_mapping'][5]['mapping'] == [{'type': 'from_text'},
                                                  {'type': 'from_entity', 'entity': 'feedback'}]

    response = client.get(
        f"/api/bot/{pytest.bot}/response/all",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    saved_responses = {response['name'] for response in actual["data"]}
    assert len({'utter_ask_restaurant_form_name', 'utter_ask_restaurant_form_num_people',
                'utter_ask_restaurant_form_cuisine', 'utter_ask_restaurant_form_outdoor_seating',
                'utter_ask_restaurant_form_preferences', 'utter_ask_restaurant_form_feedback'}.difference(
        saved_responses)) == 0
    assert actual["success"]
    assert actual["error_code"] == 0


def test_add_form_slot_not_present():
    path = [{'responses': ['please give us your location?'], 'slot': 'location',
             'mapping': [{'type': 'from_text', 'value': 'user', 'entity': 'name'},
                         {'type': 'from_entity', 'entity': 'name'}]},
            {'responses': ['seats required?'], 'slot': 'num_people',
             'mapping': [{'type': 'from_entity', 'intent': ['inform', 'request_restaurant'], 'entity': 'number'}]},
            {'responses': ['type of cuisine?'], 'slot': 'cuisine',
             'mapping': [{'type': 'from_entity', 'entity': 'cuisine'}]},
            {'responses': ['outdoor seating required?'], 'slot': 'outdoor_seating',
             'mapping': [{'type': 'from_entity', 'entity': 'seating'},
                         {'type': 'from_intent', 'intent': ['affirm'], 'value': True},
                         {'type': 'from_intent', 'intent': ['deny'], 'value': False}]},
            {'responses': ['any preferences?'], 'slot': 'preferences',
             'mapping': [{'type': 'from_text', 'not_intent': ['affirm']},
                         {'type': 'from_intent', 'intent': ['affirm'], 'value': 'no additional preferences'}]},
            {'responses': ['Please give your feedback on your experience so far'], 'slot': 'feedback',
             'mapping': [{'type': 'from_text'},
                         {'type': 'from_entity', 'entity': 'feedback'}]},
            ]
    request = {'name': 'know_user', 'path': path}
    response = client.post(
        f"/api/bot/{pytest.bot}/forms",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"].__contains__('slots not exists: {')


def test_add_form_with_validations():
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "age", "type": "float"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]

    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "location", "type": "text"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]

    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "occupation", "type": "text"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]
    name_validation = {'logical_operator': 'and',
                       'expressions': [{'validations': [{'operator': 'has_length_greater_than', 'value': 1},
                                                        {'operator': 'has_no_whitespace'}]}]}
    age_validation = {'logical_operator': 'and',
                      'expressions': [{'validations': [{'operator': '>', 'value': 10},
                                                       {'operator': '<', 'value': 70},
                                                       {'operator': 'startswith', 'value': 'valid'},
                                                       {'operator': 'endswith', 'value': 'value'}]}]}
    occupation_validation = {'logical_operator': 'and', 'expressions': [
        {'logical_operator': 'and',
         'validations': [{'operator': 'in', 'value': ['teacher', 'programmer', 'student', 'manager']},
                         {'operator': 'has_no_whitespace'},
                         {'operator': 'endswith', 'value': 'value'}]},
        {'logical_operator': 'or',
         'validations': [{'operator': 'has_length_greater_than', 'value': 20},
                         {'operator': 'has_no_whitespace'},
                         {'operator': 'matches_regex', 'value': '^[e]+.*[e]$'}]}]}
    path = [{'responses': ['what is your name?', 'name?'], 'slot': 'name',
             'mapping': [{'type': 'from_text', 'value': 'user', 'entity': 'name'},
                         {'type': 'from_entity', 'entity': 'name'}],
             'validation': name_validation,
             'utter_msg_on_valid': 'got it',
             'utter_msg_on_invalid': 'please rephrase'},
            {'responses': ['what is your age?', 'age?'], 'slot': 'age',
             'mapping': [{'type': 'from_intent', 'intent': ['get_age'], 'entity': 'age', 'value': '18'}],
             'validation': age_validation,
             'utter_msg_on_valid': 'valid entry',
             'utter_msg_on_invalid': 'please enter again'
             },
            {'responses': ['what is your location?', 'location?'], 'slot': 'location',
             'mapping': [{'type': 'from_entity', 'entity': 'location'}],
             },
            {'responses': ['what is your occupation?', 'occupation?'], 'slot': 'occupation',
             'mapping': [
                 {'type': 'from_intent', 'intent': ['get_occupation'], 'entity': 'occupation', 'value': 'business'},
                 {'type': 'from_text', 'entity': 'occupation', 'value': 'engineer'},
                 {'type': 'from_entity', 'entity': 'occupation'},
                 {'type': 'from_trigger_intent', 'entity': 'occupation', 'value': 'tester',
                  'intent': ['get_business', 'is_engineer', 'is_tester'], 'not_intent': ['get_age', 'get_name']}],
             'validation': occupation_validation}]
    request = {'name': 'know_user_form', 'path': path}
    response = client.post(
        f"/api/bot/{pytest.bot}/forms",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Form added"


def test_get_form_with_validations():
    response = client.get(
        f"/api/bot/{pytest.bot}/forms/know_user_form",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    form = actual["data"]
    assert len(form['mapping']) == 4
    assert form['slot_mapping'][0]['slot'] == 'name'
    assert form['slot_mapping'][1]['slot'] == 'age'
    assert form['slot_mapping'][2]['slot'] == 'location'
    assert form['slot_mapping'][3]['slot'] == 'occupation'
    assert form['slot_mapping'][0]['utterance'][0]['_id']
    assert form['slot_mapping'][1]['utterance'][0]['_id']
    assert form['slot_mapping'][2]['utterance'][0]['_id']
    assert form['slot_mapping'][0]['utterance'][0]['value']['text']
    assert form['slot_mapping'][1]['utterance'][0]['value']['text']
    assert form['slot_mapping'][2]['utterance'][0]['value']['text']
    assert form['slot_mapping'][3]['utterance'][0]['value']['text']
    assert form['slot_mapping'][0]['mapping'] == [{'type': 'from_text', 'value': 'user'},
                                                  {'type': 'from_entity', 'entity': 'name'}]
    assert form['slot_mapping'][1]['mapping'] == [{'type': 'from_intent', 'value': '18', 'intent': ['get_age']}]
    assert form['slot_mapping'][2]['mapping'] == [{'type': 'from_entity', 'entity': 'location'}]
    assert form['slot_mapping'][3]['mapping'] == [
        {'type': 'from_intent', 'value': 'business', 'intent': ['get_occupation']},
        {'type': 'from_text', 'value': 'engineer'}, {'type': 'from_entity', 'entity': 'occupation'},
        {'type': 'from_trigger_intent', 'value': 'tester', 'intent': ['get_business', 'is_engineer', 'is_tester'],
         'not_intent': ['get_age', 'get_name']}]

    assert form['slot_mapping'][0]['validations'] == {
        'and': [{'operator': 'has_length_greater_than', 'value': 1}, {'operator': 'has_no_whitespace', 'value': None}]}
    assert form['slot_mapping'][1]['validations'] == {
        'and': [{'operator': '>', 'value': 10}, {'operator': '<', 'value': 70},
                {'operator': 'startswith', 'value': 'valid'}, {'operator': 'endswith', 'value': 'value'}]}
    assert not form['slot_mapping'][2]['validations']
    assert form['slot_mapping'][3]['validations'] == {'and': [{'and': [
        {'operator': 'in', 'value': ['teacher', 'programmer', 'student', 'manager']},
        {'operator': 'has_no_whitespace', 'value': None}, {'operator': 'endswith', 'value': 'value'}]}, {'or': [
        {'operator': 'has_length_greater_than', 'value': 20}, {'operator': 'has_no_whitespace', 'value': None},
        {'operator': 'matches_regex', 'value': '^[e]+.*[e]$'}]}]}


def test_edit_form_add_validations():
    name_validation = {'logical_operator': 'and',
                       'expressions': [{'validations': [{'operator': 'has_length_greater_than', 'value': 4},
                                                        {'operator': 'has_no_whitespace'}]}]}
    num_people_validation = {'logical_operator': 'and',
                             'expressions': [{'validations': [{'operator': '>', 'value': 1},
                                                              {'operator': '<', 'value': 10}]}]}
    path = [{'responses': ['please give us your name?'], 'slot': 'name',
             'mapping': [{'type': 'from_text', 'value': 'user', 'entity': 'name'},
                         {'type': 'from_entity', 'entity': 'name'}],
             'validation': name_validation},
            {'responses': ['seats required?'], 'slot': 'num_people',
             'mapping': [{'type': 'from_entity', 'intent': ['inform', 'request_restaurant'], 'entity': 'number'}],
             'validation': num_people_validation,
             'utter_msg_on_valid': 'valid value',
             'utter_msg_on_invalid': 'invalid value. please enter again'},
            {'responses': ['type of cuisine?'], 'slot': 'cuisine',
             'mapping': [{'type': 'from_entity', 'entity': 'cuisine'}]},
            {'responses': ['outdoor seating required?'], 'slot': 'outdoor_seating',
             'mapping': [{'type': 'from_entity', 'entity': 'seating'},
                         {'type': 'from_intent', 'intent': ['affirm'], 'value': True},
                         {'type': 'from_intent', 'intent': ['deny'], 'value': False}]},
            {'responses': ['any preferences?'], 'slot': 'preferences',
             'mapping': [{'type': 'from_text', 'not_intent': ['affirm']},
                         {'type': 'from_intent', 'intent': ['affirm'], 'value': 'no additional preferences'}]},
            {'responses': ['Please give your feedback on your experience so far'], 'slot': 'feedback',
             'mapping': [{'type': 'from_text'},
                         {'type': 'from_entity', 'entity': 'feedback'}]},
            ]
    request = {'name': 'restaurant_form', 'path': path}
    response = client.put(
        f"/api/bot/{pytest.bot}/forms",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Form updated"


def test_edit_form_remove_validations():
    path = [{'responses': ['what is your name?', 'name?'], 'slot': 'name',
             'mapping': [{'type': 'from_text', 'value': 'user', 'entity': 'name'},
                         {'type': 'from_entity', 'entity': 'name'}],
             'utter_msg_on_valid': 'got it',
             'utter_msg_on_invalid': 'please rephrase'},
            {'responses': ['what is your age?', 'age?'], 'slot': 'age',
             'mapping': [{'type': 'from_intent', 'intent': ['get_age'], 'entity': 'age', 'value': '18'}],
             'utter_msg_on_valid': 'valid entry',
             'utter_msg_on_invalid': 'please enter again'
             },
            {'responses': ['what is your location?', 'location?'], 'slot': 'location',
             'mapping': [{'type': 'from_entity', 'entity': 'location'}],
             },
            {'responses': ['what is your occupation?', 'occupation?'], 'slot': 'occupation',
             'mapping': [
                 {'type': 'from_intent', 'intent': ['get_occupation'], 'entity': 'occupation', 'value': 'business'},
                 {'type': 'from_text', 'entity': 'occupation', 'value': 'engineer'},
                 {'type': 'from_entity', 'entity': 'occupation'},
                 {'type': 'from_trigger_intent', 'entity': 'occupation', 'value': 'tester',
                  'intent': ['get_business', 'is_engineer', 'is_tester'], 'not_intent': ['get_age', 'get_name']}]}]
    request = {'name': 'know_user_form', 'path': path}
    response = client.put(
        f"/api/bot/{pytest.bot}/forms",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Form updated"


def test_list_form():
    response = client.get(
        f"/api/bot/{pytest.bot}/forms",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] == ['restaurant_form', 'know_user_form']


def test_get_form_after_edit():
    response = client.get(
        f"/api/bot/{pytest.bot}/forms/restaurant_form",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    form = actual["data"]
    assert len(form['mapping']) == 6
    assert form['slot_mapping'][0]['slot'] == 'name'
    assert form['slot_mapping'][1]['slot'] == 'num_people'
    assert form['slot_mapping'][2]['slot'] == 'cuisine'
    assert form['slot_mapping'][3]['slot'] == 'outdoor_seating'
    assert form['slot_mapping'][4]['slot'] == 'preferences'
    assert form['slot_mapping'][5]['slot'] == 'feedback'
    assert form['slot_mapping'][0]['utterance'][0]['_id']
    assert form['slot_mapping'][1]['utterance'][0]['_id']
    assert form['slot_mapping'][2]['utterance'][0]['_id']
    assert form['slot_mapping'][3]['utterance'][0]['_id']
    assert form['slot_mapping'][4]['utterance'][0]['_id']
    assert form['slot_mapping'][5]['utterance'][0]['_id']
    assert form['slot_mapping'][0]['utterance'][0]['value']['text'] == 'please give us your name?'
    assert form['slot_mapping'][1]['utterance'][0]['value']['text'] == 'seats required?'
    assert form['slot_mapping'][2]['utterance'][0]['value']['text'] == 'type of cuisine?'
    assert form['slot_mapping'][3]['utterance'][0]['value']['text'] == 'outdoor seating required?'
    assert form['slot_mapping'][4]['utterance'][0]['value']['text'] == 'any preferences?'
    assert form['slot_mapping'][5]['utterance'][0]['value'][
               'text'] == 'Please give your feedback on your experience so far'
    assert form['slot_mapping'][0]['mapping'] == [{'type': 'from_text', 'value': 'user'},
                                                  {'type': 'from_entity', 'entity': 'name'}]
    assert form['slot_mapping'][1]['mapping'] == [
        {'type': 'from_entity', 'intent': ['inform', 'request_restaurant'], 'entity': 'number'}]
    assert form['slot_mapping'][2]['mapping'] == [{'type': 'from_entity', 'entity': 'cuisine'}]
    assert form['slot_mapping'][3]['mapping'] == [{'type': 'from_entity', 'entity': 'seating'},
                                                  {'type': 'from_intent', 'intent': ['affirm'], 'value': True},
                                                  {'type': 'from_intent', 'intent': ['deny'], 'value': False}]
    assert form['slot_mapping'][4]['mapping'] == [{'type': 'from_text', 'not_intent': ['affirm']},
                                                  {'type': 'from_intent', 'intent': ['affirm'],
                                                   'value': 'no additional preferences'}]
    assert form['slot_mapping'][5]['mapping'] == [{'type': 'from_text'},
                                                  {'type': 'from_entity', 'entity': 'feedback'}]
    assert form['slot_mapping'][0]['validations'] == {
        'and': [{'operator': 'has_length_greater_than', 'value': 4}, {'operator': 'has_no_whitespace', 'value': None}]}
    assert form['slot_mapping'][1]['validations'] == {
        'and': [{'operator': '>', 'value': 1}, {'operator': '<', 'value': 10}]}
    assert not form['slot_mapping'][2]['validations']
    assert not form['slot_mapping'][3]['validations']
    assert not form['slot_mapping'][4]['validations']

    response = client.get(
        f"/api/bot/{pytest.bot}/response/all",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    saved_responses = {response['name'] for response in actual["data"]}
    assert len({'utter_ask_restaurant_form_name', 'utter_ask_restaurant_form_num_people',
                'utter_ask_restaurant_form_cuisine', 'utter_ask_restaurant_form_outdoor_seating',
                'utter_ask_restaurant_form_preferences', 'utter_ask_restaurant_form_feedback'}.difference(
        saved_responses)) == 0
    assert actual["success"]
    assert actual["error_code"] == 0


def test_edit_form():
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "ac_required", "type": "text"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["message"] == "Slot added successfully!"
    assert actual["success"]

    path = [{'responses': ['which location would you prefer?'], 'slot': 'location',
             'mapping': [{'type': 'from_text', 'value': 'user', 'entity': 'location'},
                         {'type': 'from_entity', 'entity': 'location'}]},
            {'responses': ['seats required?'], 'slot': 'num_people',
             'mapping': [{'type': 'from_entity', 'intent': ['inform', 'request_restaurant'], 'entity': 'number'}]},
            {'responses': ['type of cuisine?'], 'slot': 'cuisine',
             'mapping': [{'type': 'from_entity', 'entity': 'cuisine'}]},
            {'responses': ['outdoor seating required?'], 'slot': 'outdoor_seating',
             'mapping': [{'type': 'from_entity', 'entity': 'seating'},
                         {'type': 'from_intent', 'intent': ['affirm'], 'value': True},
                         {'type': 'from_intent', 'intent': ['deny'], 'value': False}]},
            {'responses': ['any preferences?'], 'slot': 'preferences',
             'mapping': [{'type': 'from_text', 'not_intent': ['affirm']},
                         {'type': 'from_intent', 'intent': ['affirm'], 'value': 'no additional preferences'}]},
            {'responses': ['do you want to go with an AC room?'], 'slot': 'ac_required',
             'mapping': [{'type': 'from_intent', 'intent': ['affirm'], 'value': True},
                         {'type': 'from_intent', 'intent': ['deny'], 'value': False}]},
            {'responses': ['Please give your feedback on your experience so far'], 'slot': 'feedback',
             'mapping': [{'type': 'from_text'},
                         {'type': 'from_entity', 'entity': 'feedback'}]}
            ]
    request = {'name': 'restaurant_form', 'path': path}
    response = client.put(
        f"/api/bot/{pytest.bot}/forms",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Form updated"


def test_delete_form():
    response = client.delete(
        f"/api/bot/{pytest.bot}/forms",
        json={'data': 'restaurant_form'},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Form deleted"


def test_delete_form_already_deleted():
    response = client.delete(
        f"/api/bot/{pytest.bot}/forms",
        json={'data': 'restaurant_form'},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Form "restaurant_form" does not exists'


def test_delete_form_not_exists():
    response = client.delete(
        f"/api/bot/{pytest.bot}/forms",
        json={'data': 'form_not_exists'},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Form "form_not_exists" does not exists'


def test_add_slot_set_action():
    request = {'name': 'action_set_name_slot', 'slot': 'name', 'type': 'from_value', 'value': 5}
    response = client.post(
        f"/api/bot/{pytest.bot}/action/slotset",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Action added"


def test_add_slot_set_action_slot_not_exists():
    request = {'name': 'action_set_new_user_slot', 'slot': 'new_user', 'type': 'from_value', 'value': False}
    response = client.post(
        f"/api/bot/{pytest.bot}/action/slotset",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Slot with name "new_user" not found'


def test_list_slot_set_actions():
    response = client.get(
        f"/api/bot/{pytest.bot}/action/slotset",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual["data"]) == 1
    assert actual["data"][0] == {'name': 'action_set_name_slot', 'slot': 'name', 'type': 'from_value', 'value': 5}


def test_edit_slot_set_action():
    request = {'name': 'action_set_name_slot', 'slot': 'name', 'type': 'from_value', 'value': 'age'}
    response = client.put(
        f"/api/bot/{pytest.bot}/action/slotset",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == 'Action updated'


def test_edit_slot_set_action_slot_not_exists():
    request = {'name': 'action_set_name_slot', 'slot': 'non_existant', 'type': 'from_value', 'value': 'age'}
    response = client.put(
        f"/api/bot/{pytest.bot}/action/slotset",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Slot with name "non_existant" not found'


def test_delete_slot_set_action_not_exists():
    response = client.delete(
        f"/api/bot/{pytest.bot}/action/slotset/non_existant",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Action with name "non_existant" not found'


def test_delete_slot_set_action():
    response = client.delete(
        f"/api/bot/{pytest.bot}/action/slotset/action_set_name_slot",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == 'Action deleted'


def test_list_slot_set_action_none_present():
    response = client.get(
        f"/api/bot/{pytest.bot}/action/slotset",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"] == []


def test_add_intent_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/intents",
        json={"data": "CASE_INSENSITIVE_INTENT"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Intent added successfully!"

    response = client.get(
        f"/api/bot/{pytest.bot}/intents",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    intents_added = [i['name'] for i in actual["data"]]
    assert 'CASE_INSENSITIVE_INTENT' not in intents_added
    assert 'case_insensitive_intent' in intents_added
    assert actual["success"]
    assert actual["error_code"] == 0

    response = client.post(
        f"/api/bot/{pytest.bot}/training_examples/CASE_INSENSITIVE_INTENT",
        json={"data": ["IS THIS CASE_INSENSITIVE_INTENT?"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"][0]["message"] == "Training Example added"

    response = client.get(
        f"/api/bot/{pytest.bot}/training_examples/case_insensitive_intent",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    training_examples = [t['text'] for t in actual["data"]]
    assert "IS THIS CASE_INSENSITIVE_INTENT?" in training_examples
    assert actual["success"]
    assert actual["error_code"] == 0


def test_add_training_example_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/training_examples/CASE_INSENSITIVE_TRAINING_EX_INTENT",
        json={"data": ["IS THIS CASE_INSENSITIVE_TRAINING_EX_INTENT?"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"][0]["message"] == "Training Example added"

    response = client.get(
        f"/api/bot/{pytest.bot}/training_examples/case_insensitive_training_ex_intent",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "IS THIS CASE_INSENSITIVE_TRAINING_EX_INTENT?" in [t['text'] for t in actual["data"]]
    assert actual["success"]
    assert actual["error_code"] == 0


def test_add_utterances_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/utterance",
        json={"data": "utter_CASE_INSENSITIVE_UTTERANCE"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Utterance added!"

    response = client.get(
        f"/api/bot/{pytest.bot}/utterance",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    utterances_added = [u['name'] for u in actual['data']['utterances']]
    assert 'utter_CASE_INSENSITIVE_UTTERANCE' not in utterances_added
    assert 'utter_case_insensitive_utterance' in utterances_added


def test_add_responses_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/response/utter_CASE_INSENSITIVE_RESPONSE",
        json={"data": "yes, this is utter_CASE_INSENSITIVE_RESPONSE"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Response added!"

    response = client.get(
        f"/api/bot/{pytest.bot}/response/utter_CASE_INSENSITIVE_RESPONSE",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"][0]['value'] == {'text': 'yes, this is utter_CASE_INSENSITIVE_RESPONSE'}

    response = client.get(
        f"/api/bot/{pytest.bot}/response/utter_case_insensitive_response",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual["data"]) == 1


def test_add_story_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "CASE_INSENSITIVE_STORY",
            "type": "STORY",
            "template_type": "Q&A",
            "steps": [
                {"name": "case_insensitive_training_ex_intent", "type": "INTENT"},
                {"name": "utter_case_insensitive_response", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["message"] == "Flow added successfully"
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0

    response = client.get(
        f"/api/bot/{pytest.bot}/stories",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]
    assert Utility.check_empty_string(actual["message"])
    stories_added = [s['name'] for s in actual["data"]]
    assert 'CASE_INSENSITIVE_STORY' not in stories_added
    assert 'case_insensitive_story' in stories_added

    response = client.delete(
        f"/api/bot/{pytest.bot}/stories/case_insensitive_story/STORY",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Flow deleted successfully"


def test_add_rule_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/stories",
        json={
            "name": "CASE_INSENSITIVE_RULE",
            "type": "RULE",
            "steps": [
                {"name": "case_insensitive_training_ex_intent", "type": "INTENT"},
                {"name": "utter_case_insensitive_response", "type": "BOT"},
            ],
        },
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["message"] == "Flow added successfully"
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0

    response = client.get(
        f"/api/bot/{pytest.bot}/stories",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["data"]
    assert Utility.check_empty_string(actual["message"])
    stories_added = [s['name'] for s in actual["data"]]
    assert 'CASE_INSENSITIVE_RULE' not in stories_added
    assert 'case_insensitive_rule' in stories_added


def test_add_regex_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/regex",
        json={"name": "CASE_INSENSITIVE_REGEX", "pattern": "b*b"},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Regex pattern added successfully!"

    response = client.get(
        f"/api/bot/{pytest.bot}/regex",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert Utility.check_empty_string(actual["message"])
    assert "CASE_INSENSITIVE_REGEX" != actual['data'][0]['name']
    assert "case_insensitive_regex" == actual['data'][0]['name']


def test_add_lookup_table_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/lookup/tables",
        json={"name": "CASE_INSENSITIVE_LOOKUP", "value": ["test1", "test2"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Lookup table and values added successfully!"

    response = client.get(
        f"/api/bot/{pytest.bot}/lookup/tables",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    lookups_added = [l['name'] for l in actual['data']]
    assert 'CASE_INSENSITIVE_LOOKUP' not in lookups_added
    assert 'case_insensitive_lookup' in lookups_added


def test_add_entity_synonym_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        json={"name": "CASE_INSENSITIVE", "value": ["CASE_INSENSITIVE_SYNONYM"]},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Synonym and values added successfully!"

    response = client.get(
        f"/api/bot/{pytest.bot}/entity/synonyms",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual['data'] == [{'CASE_INSENSITIVE_SYNONYM': 'case_insensitive'}]


def test_add_slot_case_insensitivity():
    response = client.post(
        f"/api/bot/{pytest.bot}/slots",
        json={"name": "CASE_INSENSITIVE_SLOT", "type": "any", "initial_value": "bot", "influence_conversation": False},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert "data" in actual
    assert actual["message"] == "Slot added successfully!"
    assert actual["data"]["_id"]
    assert actual["success"]
    assert actual["error_code"] == 0

    response = client.get(
        f"/api/bot/{pytest.bot}/slots",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert "data" in actual
    assert len(actual["data"])
    assert actual["success"]
    assert actual["error_code"] == 0


def test_add_form_case_insensitivity():
    path = [{'responses': ['please give us your name?'], 'slot': 'name',
             'mapping': [{'type': 'from_text', 'value': 'user', 'entity': 'name'},
                         {'type': 'from_entity', 'entity': 'name'}]},
            ]
    request = {'name': 'CASE_INSENSITIVE_FORM', 'path': path}
    response = client.post(
        f"/api/bot/{pytest.bot}/forms",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Form added"

    response = client.get(
        f"/api/bot/{pytest.bot}/forms/CASE_INSENSITIVE_FORM",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert not actual["success"]
    assert actual["error_code"] == 422
    assert actual["message"] == 'Form does not exists'

    response = client.get(
        f"/api/bot/{pytest.bot}/forms/case_insensitive_form",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual['data']


def test_add_slot_set_action_case_insensitivity():
    request = {'name': 'CASE_INSENSITIVE_SLOT_SET_ACTION', 'slot': 'name', 'type': 'from_value', 'value': 5}
    response = client.post(
        f"/api/bot/{pytest.bot}/action/slotset",
        json=request,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert actual["message"] == "Action added"

    response = client.get(
        f"/api/bot/{pytest.bot}/action/slotset",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["success"]
    assert actual["error_code"] == 0
    assert len(actual["data"]) == 1
    assert actual["data"][0] == {'name': 'case_insensitive_slot_set_action', 'slot': 'name', 'type': 'from_value',
                                 'value': 5}


def test_add_http_action_case_insensitivity():
    request_body = {
        "action_name": "CASE_INSENSITIVE_HTTP_ACTION",
        "response": "string",
        "http_url": "http://www.google.com",
        "request_method": "GET",
        "params_list": [{
            "key": "testParam1",
            "parameter_type": "value",
            "value": "testValue1"
        }]
    }

    response = client.post(
        url=f"/api/bot/{pytest.bot}/action/httpaction",
        json=request_body,
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )

    actual = response.json()
    assert actual["error_code"] == 0
    assert actual["message"]
    assert actual["success"]

    response = client.get(
        url=f"/api/bot/{pytest.bot}/action/httpaction/CASE_INSENSITIVE_HTTP_ACTION",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert not actual['data']

    response = client.get(
        url=f"/api/bot/{pytest.bot}/action/httpaction/case_insensitive_http_action",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual['data']
    assert actual["success"]


def test_get_ui_config_empty():
    response = client.get(
        url=f"/api/account/config/ui",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual['data'] == {}
    assert actual["success"]


def test_add_ui_config():
    response = client.put(
        url=f"/api/account/config/ui",
        json={'data': {'has_stepper': True, 'has_tour': False, 'theme': 'white'}},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert not actual['data']
    assert actual["success"]
    assert actual["message"] == 'Config saved!'

    response = client.put(
        url=f"/api/account/config/ui",
        json={'data': {'has_stepper': True, 'has_tour': False, 'theme': 'black'}},
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert not actual['data']
    assert actual["success"]
    assert actual["message"] == 'Config saved!'


def test_get_ui_config():
    response = client.get(
        url=f"/api/account/config/ui",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 0
    assert actual['data'] == {'has_stepper': True, 'has_tour': False, 'theme': 'black'}
    assert actual["success"]


def test_model_testing_no_existing_models():
    response = client.post(
        url=f"/api/bot/{pytest.bot}/test",
        headers={"Authorization": pytest.token_type + " " + pytest.access_token},
    )
    actual = response.json()
    assert actual["error_code"] == 422
    assert actual['message'] == 'No model trained yet. Please train a model to test'
    assert not actual["success"]
