import os
import shutil
from datetime import datetime
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = 'Cria um backup de segurança do banco de dados SQLite'

    def handle(self, *args, **options):
        import subprocess
        from django.db import connection
        
        backup_dir = os.path.join(settings.BASE_DIR, 'backups')
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
            
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Verificar qual banco está sendo usado
        db_engine = settings.DATABASES['default']['ENGINE']
        
        if 'postgresql' in db_engine:
            # Lógica para PostgreSQL (Produção)
            backup_filename = f'db_backup_{timestamp}.sql.gz'
            backup_path = os.path.join(backup_dir, backup_filename)
            
            # Pega a URL do banco do ambiente (Railway usa DATABASE_URL)
            db_url = os.environ.get('DATABASE_URL')
            
            if not db_url:
                self.stdout.write(self.style.ERROR('DATABASE_URL não encontrada para o backup do Postgres.'))
                return

            try:
                # Executa o pg_dump com formato Custom (-Fc), sem ACL/owners
                self.stdout.write("Executando pg_dump (formato Custom)...")
                process = subprocess.run(
                    ['pg_dump', '-Fc', '--no-acl', '--no-owner', db_url, '-f', backup_path],
                    capture_output=True,
                    text=True,
                    timeout=600
                )
                
                if process.returncode == 0:
                    # Verifica o tamanho do arquivo
                    file_size = os.path.getsize(backup_path)
                    if file_size < 100:
                        self.stdout.write(self.style.ERROR(f'Erro: O backup gerado é muito pequeno ({file_size} bytes).'))
                        return
                    self.stdout.write(self.style.SUCCESS(f'Backup Postgres realizado: {backup_filename} ({file_size} bytes)'))
                else:
                    self.stdout.write(self.style.ERROR(f'Erro no pg_dump (retorno {process.returncode}): {process.stderr}'))
                    return
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Falha ao executar pg_dump: {e}'))
                return
        else:
            # Lógica para SQLite (Local)
            db_path = os.path.join(settings.BASE_DIR, 'db.sqlite3')
            backup_filename = f'db_backup_{timestamp}.sqlite3'
            backup_path = os.path.join(backup_dir, backup_filename)
            
            try:
                shutil.copy2(db_path, backup_path)
                self.stdout.write(self.style.SUCCESS(f'Backup SQLite realizado: {backup_filename}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Erro no backup SQLite: {e}'))
                return

        # Limpeza de backups antigos (mantém os últimos 200)
        # Nota: Agora que estamos no S3, a limpeza local é imediata após o upload.
        # Se quiser limpar o S3, teria que implementar lógica de lifecycle no bucket.
        
        # Enviar para o S3
        self.upload_to_s3(backup_path, backup_filename)
        
        # Remover o arquivo local após o upload para não ocupar espaço no Railway
        if os.path.exists(backup_path):
            os.remove(backup_path)

    def upload_to_s3(self, file_path, file_name):
        import boto3
        from botocore.exceptions import NoCredentialsError

        s3_key = os.environ.get('AWS_ACCESS_KEY_ID')
        s3_secret = os.environ.get('AWS_SECRET_ACCESS_KEY')
        bucket_name = os.environ.get('AWS_STORAGE_BUCKET_NAME')
        region = os.environ.get('AWS_S3_REGION_NAME', 'us-east-1')
        endpoint = os.environ.get('AWS_S3_ENDPOINT_URL') # Opcional (para Cloudflare R2, MinIO, etc)

        if not all([s3_key, s3_secret, bucket_name]):
            self.stdout.write(self.style.WARNING('Credenciais S3 não configuradas. Backup mantido apenas localmente (será apagado no deploy).'))
            return

        s3 = boto3.client(
            's3',
            aws_access_key_id=s3_key,
            aws_secret_access_key=s3_secret,
            region_name=region,
            endpoint_url=endpoint
        )

        try:
            self.stdout.write(f'Enviando {file_name} para o S3...')
            s3.upload_file(file_path, bucket_name, f'backups/{file_name}')
            self.stdout.write(self.style.SUCCESS(f'Backup enviado com sucesso para o S3: {bucket_name}/backups/{file_name}'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Falha ao enviar para o S3: {str(e)}'))

