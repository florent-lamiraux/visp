import os
from pathlib import Path
import json
import subprocess
from subprocess import CalledProcessError
import os
megapose_url = 'https://github.com/megapose6d/megapose6d.git'

def get_pip_for_conda_env(megapose_env: str):
  env_data = str(subprocess.check_output('conda info --envs', shell=True).decode())
  env_lines = env_data.split('\n')
  megapose_env_line = [line for line in env_lines if line.startswith(megapose_env)]
  assert(len(megapose_env_line) == 1, 'Found multiple environment names with same name, shouldnt happen')
  megapose_env_line = megapose_env_line[0]
  megapose_env_path = Path(megapose_env_line.split()[-1])
  assert(megapose_env_path.exists())
  megapose_env_pip = megapose_env_path / 'bin' / 'pip'
  assert(megapose_env_pip.exists(), 'Could not find pip in conda env directory')
  return megapose_env_pip

def clone_megapose(megapose_path: Path):
  print('Cloning megapose git repo...')
  try:
    subprocess.run(['git', 'clone', megapose_url, str(megapose_path)], check=True, text=True)
    current_dir = os.getcwd()
    os.chdir(megapose_path)
    subprocess.run(['git', 'submodule', 'update', '--init'], check=True, text=True)
    os.chdir(current_dir)
  except CalledProcessError as e:
    print('Could not clone megapose directory')
    exit(1)

def install_dependencies(megapose_path: Path, megapose_environment: str):
  try:
    subprocess.run(['conda', 'env', 'create', '--name', megapose_environment, '--file', str(megapose_path / 'conda/environment_full.yaml')], check=True)
    megapose_env_pip = get_pip_for_conda_env(megapose_environment)
    subprocess.run([megapose_env_pip, 'install', '-e',  str(megapose_path)], check=True)
  except CalledProcessError as e:
    print('Could create conda environment')
    exit(1)


def download_models(megapose_path: Path, megapose_data_path: Path):
  models_path = megapose_data_path / 'megapose-models'
  conf_path = megapose_path / 'rclone.conf'
  arguments = ['rclone', 'copyto', 'megapose_public_readonly:/megapose-models',
                   str(models_path), '--exclude', '"**epoch**"', '--config', str(conf_path), '--progress']
  print(' '.join(arguments))
  subprocess.run(arguments, check=True)


def install_server(megapose_env: str):
  megapose_env_pip = get_pip_for_conda_env(megapose_env)
  subprocess.run([megapose_env_pip, 'install',  '.'], check=True)



if __name__ == "__main__":
  megapose_variables = None
  with open('./megapose_variables.json', 'r') as variables:
    megapose_variables = json.load(variables)


  megapose_server_dir = Path(os.path.dirname(os.path.abspath(__file__)))


  megapose_dir = Path(megapose_variables['megapose_dir']).absolute()
  megapose_data_dir = Path(megapose_variables['megapose_data_dir']).absolute()
  megapose_environment = megapose_variables['environment']

  display_message = f'''
This script installs Megapose and the server to communicate with ViSP.
the file "megapose_variables.json" specifies where megapose should be cloned and where the models should be downloaded.
It also contains the name of the conda environment to create.

current values:
  - Megapose directory: {megapose_dir}
  - Megapose model directory: {megapose_data_dir}
  - Conda environment name: {megapose_environment}

Installation requires:
  - git
  - conda
  - rclone (see: https://rclone.org/install/)

All these programs should be in your path.

It will create a new environment called megapose, with all dependencies installed.
All the megapose models will be downloaded via rclone.

If you encounter any problem, see https://github.com/megapose6d/megapose6d for the installation steps.

The steps followed in the script are the same, but end with the installation of the megapose_server package (the python scripts in this folder)

'''
  print(display_message)
  print('Cloning megapose directory...')
  # clone_megapose(megapose_dir)
  # print('Creating conda environment and installing megapose...')
  # install_dependencies(megapose_dir, megapose_environment)
  # print('Downloading megapose models...')
  # download_models(megapose_dir, megapose_data_dir)
  # print('Installing server...')
  install_server(megapose_environment)

  print(f'''
Megapose server is now installed!
Try:
  $ conda activate {megapose_environment}
  $ python -m megapose_server.run -h
  ''')


