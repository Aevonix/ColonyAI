"""One isolated Hermes CLI-loop attempt, launched only by qualification.native."""
import json
from pathlib import Path
import signal
import sys
import threading


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    state = Path(sys.argv[1]).parent
    stop = threading.Event()
    owned = {}
    result = {'stage': 'constructing', 'worker_stopped': False,
              'agent_close_returned': False, 'hard_interrupt_requested': False}

    def interrupt(_signum, _frame):
        stop.set()
        agent = owned.get('agent')
        if agent is not None:
            result['hard_interrupt_requested'] = True
            agent.hard_interrupt('Qualification elapsed deadline')

    signal.signal(signal.SIGTERM, interrupt)

    def run():
        agent = None
        try:
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from hermes_constants import resolve_reasoning_config
            from run_agent import AIAgent
            config = load_config()
            model = config['model']['default']
            runtime = resolve_runtime_provider(requested=request['binding'], target_model=model)
            kwargs = {k: runtime[k] for k in ('base_url', 'api_key', 'provider', 'api_mode',
                'requested_provider', 'request_overrides', 'capabilities') if k in runtime}
            agent = AIAgent(model=model, **kwargs, platform='cli', max_iterations=1,
                max_tokens=request['inputs']['max_output_tokens'], enabled_toolsets=[], quiet_mode=True,
                skip_context_files=True, skip_memory=True, skip_background_review=True,
                reasoning_config=resolve_reasoning_config(config, model),
                service_tier=config.get('agent', {}).get('service_tier'),
                fallback_model=None, save_trajectories=False)
            owned['agent'] = agent
            result['model'] = agent.model
            if stop.is_set():
                interrupt(signal.SIGTERM, None)
                result['stage'] = 'interrupted'
                return
            result['stage'] = 'running'
            messages = request['inputs']['messages']
            response = agent.run_conversation(messages[-1]['content'],
                system_message='\n'.join(m['content'] for m in messages[:-1]))
            result['turn'] = {k: response.get(k) for k in ('completed', 'failed', 'interrupted', 'partial')}
            complete = response.get('completed') is True and not any(response.get(k)
                for k in ('failed', 'interrupted', 'partial'))
            result['stage'] = 'interrupted' if stop.is_set() else 'returned' if complete else 'incomplete'
            result['output'] = response.get('final_response')
        except BaseException as exc:
            result.update(stage='interrupted' if stop.is_set() else 'error', error_type=type(exc).__name__)
        finally:
            if agent is not None:
                try:
                    agent.close()
                    result['agent_close_returned'] = True
                except BaseException as exc:
                    result['close_error_type'] = type(exc).__name__

    worker = threading.Thread(target=run, name='native-qualification', daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(.05)
    result['worker_stopped'] = True
    with (state/'native-result.json').open('x') as stream:
        json.dump(result, stream, allow_nan=False)
    return 0 if result['stage'] == 'returned' else 1


if __name__ == '__main__':
    raise SystemExit(main())
