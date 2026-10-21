"""Run the installed-style utility with only hardware programs substituted."""
from array import array
import pytest
from tests.support.load_tool import load_tool


def test_optional_load_modes_and_binary_audio(tmp_path):
    load = load_tool(tmp_path)
    plain = load('--seconds', '0.08')
    combined = load('--seconds', '0.3', '--audio', 'rain', '--gpu', '--seed', '72')

    assert plain.returncode == combined.returncode == 0, combined.stderr
    assert "'gpu_batches': 0" in plain.stdout
    assert 'Test GPU (Integrated GPU)' in combined.stdout
    pcm = array('h', (tmp_path/'audio.raw').read_bytes())
    assert len(pcm) > 48000 and max(pcm) > 0 and min(pcm) < 0
    assert max(abs(sample) for sample in pcm) <= 3277
    assert not list(tmp_path.glob('quirkbench-load-*/'))


@pytest.mark.parametrize('gpu_result,audio_failure,options', [
    ('skip', False, ['--gpu']), ('pass', True, ['--audio', 'rain'])])
def test_requested_load_failure_is_not_success(tmp_path, gpu_result, audio_failure, options):
    load = load_tool(tmp_path, gpu_result=gpu_result, audio_failure=audio_failure)
    result = load('--seconds', '0.3', *options)
    assert result.returncode != 0 and 'Load failed:' in result.stderr
    assert 'Load finished:' not in result.stdout


def test_stop_interrupts_long_load(tmp_path):
    load = load_tool(tmp_path)
    with load.start('--seconds', '300', '--gpu', '--audio', 'rain') as process:
        try:
            assert 'Load started:' in process.stdout.readline()
            assert 'Vulkan device' in process.stdout.readline()
            process.terminate()
            output, error = process.communicate(timeout=3)
            assert process.returncode == 130 and 'Load stopped.' in output, error
        finally:
            if process.poll() is None:
                process.kill()
