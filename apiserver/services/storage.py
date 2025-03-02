from apiserver.apimodels.storage import ResetSettingsRequest, SetSettingsRequest
from apiserver.bll.storage import StorageBLL
from apiserver.service_repo import endpoint, APICall

storage_bll = StorageBLL()


@endpoint("storage.get_settings")
def get_settings(call: APICall, company: str, _):
    call.result.data = {"settings": storage_bll.get_company_settings(company)}


@endpoint("storage.set_settings")
def set_settings(call: APICall, company: str, request: SetSettingsRequest):
    call.result.data = {"updated": storage_bll.set_company_settings(company, request)}


@endpoint("storage.reset_settings")
def reset_settings(call: APICall, company: str, request: ResetSettingsRequest):
    call.result.data = {
        "updated": storage_bll.reset_company_settings(company, request.keys)
    }
