"""交互助手默认 SenseVoice STT 工厂测试。"""

from unittest.mock import MagicMock, patch

from voice_realtime.asr.adapters.pipecat_sensevoice import PipecatSenseVoiceFactory


def test_factory_preserves_current_pipecat_configuration() -> None:
    service = MagicMock(name="stt_service")
    with (
        patch(
            "voice_realtime.asr.adapters.pipecat_sensevoice.resolve_model_snapshot",
            return_value="/models/sensevoice",
        ) as resolve,
        patch(
            "voice_realtime.asr.adapters.pipecat_sensevoice.FunASRSTTService",
            return_value=service,
        ) as service_class,
    ):
        factory = PipecatSenseVoiceFactory(
            model="FunAudioLLM/SenseVoiceSmall",
            allow_model_downloads=False,
        )

        processor = factory.create_processor(sample_rate=16000, language="EN")

    assert processor is service
    resolve.assert_called_once_with(
        "FunAudioLLM/SenseVoiceSmall",
        default_repo="FunAudioLLM/SenseVoiceSmall",
        allow_downloads=False,
    )
    assert service_class.call_args.kwargs["device"] == "cpu"
    assert service_class.call_args.kwargs["ttfs_p99_latency"] == 0.5
    stt_settings = service_class.call_args.kwargs["settings"]
    assert stt_settings.language == "en"
    assert stt_settings.use_itn is True
