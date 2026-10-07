from django.core.management.base import BaseCommand
from django.db import transaction
from api.models import Appointment, Service, WaitlistEntry

class Command(BaseCommand):
    help = 'Restaura os serviços dos agendamentos perdidos baseando-se no total_price'

    def handle(self, *args, **options):
        with transaction.atomic():
            services = list(Service.objects.all())
            if not services:
                self.stdout.write(self.style.ERROR('Nenhum serviço encontrado no banco de dados!'))
                return

            fixed_apps = 0
            # Achar todos os agendamentos sem serviço associado
            empty_apps = Appointment.objects.filter(services__isnull=True)
            total_apps = empty_apps.count()

            for app in empty_apps:
                if app.total_price:
                    closest = min(services, key=lambda s: abs(s.price - app.total_price))
                    app.services.add(closest)
                    fixed_apps += 1
                else:
                    app.services.add(services[0])
                    fixed_apps += 1

            fixed_waitlist = 0
            empty_waitlists = WaitlistEntry.objects.filter(services__isnull=True)
            total_waitlists = empty_waitlists.count()
            
            for w in empty_waitlists:
                w.services.add(services[0])
                fixed_waitlist += 1

            self.stdout.write(self.style.SUCCESS(f'Sucesso! Restaurados {fixed_apps} de {total_apps} agendamentos vazios e {fixed_waitlist} de {total_waitlists} da fila de espera.'))
