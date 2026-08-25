import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
py_files = [str(p) for p in root.rglob('*.py') if '__pycache__' not in p.parts]
subprocess.run([sys.executable, '-m', 'py_compile', *py_files], check=True)
subprocess.run([sys.executable, str(root/'tests'/'test_engine_mock.py')], cwd=root, check=True)
subprocess.run([sys.executable, str(root/'tests'/'test_capacity.py')], cwd=root, check=True)
subprocess.run([sys.executable, str(root/'tests'/'test_mapbox_request.py')], cwd=root, check=True)
subprocess.run([sys.executable, str(root/'tests'/'test_bundle_light.py')], cwd=root, check=True)
subprocess.run([sys.executable, str(root/'tests'/'test_micro_gain.py')], cwd=root, check=True)
subprocess.run([sys.executable, str(root/'tests'/'test_fastest_purity.py')], cwd=root, check=True)
subprocess.run([sys.executable, str(root/'tests'/'test_prepared_geometry.py')], cwd=root, check=True)
subprocess.run([sys.executable, str(root/'tests'/'test_cache_memory.py')], cwd=root, check=True)
print('VALIDAÇÃO VAIGO ROUTE NODE: OK')
