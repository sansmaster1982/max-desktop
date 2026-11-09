"""Client-owned message-action wording, never message text or server errors.

Columns: English, Spanish, Brazilian Portuguese, French, Uzbek, Belarusian.
These are local translations, not claimed to be official MAX catalogue entries.
"""

MESSAGE_ACTION_TRANSLATIONS = {
    'Копировать текст': ('Copy text', 'Copiar texto', 'Copiar texto', 'Copier le texte', 'Matnni nusxalash', 'Капіяваць тэкст'),
    'Ответить': ('Reply', 'Responder', 'Responder', 'Répondre', 'Javob berish', 'Адказаць'),
    'Переслать…': ('Forward…', 'Reenviar…', 'Encaminhar…', 'Transférer…', 'Boshqaga yuborish…', 'Пераслаць…'),
    'Изменить…': ('Edit…', 'Editar…', 'Editar…', 'Modifier…', 'Tahrirlash…', 'Змяніць…'),
    'Реакция': ('Reaction', 'Reacción', 'Reação', 'Réaction', 'Reaksiya', 'Рэакцыя'),
    'Убрать мою реакцию': ('Remove my reaction', 'Quitar mi reacción', 'Remover minha reação', 'Retirer ma réaction', 'Reaksiyamni olib tashlash', 'Прыбраць маю рэакцыю'),
    'Отметить прочитанным': ('Mark as read', 'Marcar como leído', 'Marcar como lido', 'Marquer comme lu', 'O‘qilgan deb belgilash', 'Пазначыць прачытаным'),
    'Удалить…': ('Delete…', 'Eliminar…', 'Excluir…', 'Supprimer…', 'O‘chirish…', 'Выдаліць…'),
    'Копировать выделенное': ('Copy selection', 'Copiar selección', 'Copiar seleção', 'Copier la sélection', 'Tanlangan matnni nusxalash', 'Капіяваць вылучанае'),
    'Выделить всё': ('Select all', 'Seleccionar todo', 'Selecionar tudo', 'Tout sélectionner', 'Hammasini tanlash', 'Вылучыць усё'),
    'Изменить сообщение': ('Edit message', 'Editar mensaje', 'Editar mensagem', 'Modifier le message', 'Xabarni tahrirlash', 'Змяніць паведамленне'),
    'Текст сообщения:': ('Message text:', 'Texto del mensaje:', 'Texto da mensagem:', 'Texte du message :', 'Xabar matni:', 'Тэкст паведамлення:'),
    'Сохранить': ('Save', 'Guardar', 'Salvar', 'Enregistrer', 'Saqlash', 'Захаваць'),
    'Отменить': ('Cancel', 'Cancelar', 'Cancelar', 'Annuler', 'Bekor qilish', 'Адмяніць'),
    'Удалить сообщение': ('Delete message', 'Eliminar mensaje', 'Excluir mensagem', 'Supprimer le message', 'Xabarni o‘chirish', 'Выдаліць паведамленне'),
    'Удалить выбранное сообщение?': ('Delete the selected message?', '¿Eliminar el mensaje seleccionado?', 'Excluir a mensagem selecionada?', 'Supprimer le message sélectionné ?', 'Tanlangan xabar o‘chirilsinmi?', 'Выдаліць выбранае паведамленне?'),
    'Удалить у меня': ('Delete for me', 'Eliminar para mí', 'Excluir para mim', 'Supprimer pour moi', 'Faqat menda o‘chirish', 'Выдаліць у мяне'),
    'Удалить у всех': ('Delete for everyone', 'Eliminar para todos', 'Excluir para todos', 'Supprimer pour tout le monde', 'Hammada o‘chirish', 'Выдаліць для ўсіх'),
    'Переслать сообщение': ('Forward message', 'Reenviar mensaje', 'Encaminhar mensagem', 'Transférer le message', 'Xabarni boshqaga yuborish', 'Пераслаць паведамленне'),
    'Выберите чат:': ('Choose a chat:', 'Elige un chat:', 'Escolha um chat:', 'Choisissez une discussion :', 'Chatni tanlang:', 'Выберыце чат:'),
    'Переслать': ('Forward', 'Reenviar', 'Encaminhar', 'Transférer', 'Boshqaga yuborish', 'Пераслаць'),
    'Прочтение не подтверждено': ('Read status not confirmed', 'No se confirmó la lectura', 'Leitura não confirmada', 'Lecture non confirmée', 'O‘qilganlik holati tasdiqlanmagan', 'Прачытанне не пацверджана'),
    'Действие не отправлено': ('Action not sent', 'Acción no enviada', 'Ação não enviada', 'Action non envoyée', 'Amal yuborilmadi', 'Дзеянне не адпраўлена'),
    'Действие отклонено': ('Action rejected', 'Acción rechazada', 'Ação recusada', 'Action refusée', 'Amal rad etildi', 'Дзеянне адхілена'),
    'Результат не подтверждён': ('Result not confirmed', 'Resultado no confirmado', 'Resultado não confirmado', 'Résultat non confirmé', 'Natija tasdiqlanmagan', 'Вынік не пацверджаны'),
    'Действие не подтверждено': ('Action not confirmed', 'Acción no confirmada', 'Ação não confirmada', 'Action non confirmée', 'Amal tasdiqlanmagan', 'Дзеянне не пацверджана'),
    'Удаление в Избранном': ('Deleting from Favorites', 'Eliminar de Favoritos', 'Exclusão em Favoritos', 'Suppression dans les Favoris', 'Tanlanganlardan o‘chirish', 'Выдаленне з «Выбранага»'),
    'Сессия изменилась. Откройте сообщение заново.': (
        'The session changed. Open the message again.', 'La sesión cambió. Vuelve a abrir el mensaje.',
        'A sessão mudou. Abra a mensagem novamente.', 'La session a changé. Ouvrez à nouveau le message.',
        'Sessiya o‘zgardi. Xabarni qayta oching.', 'Сесія змянілася. Адкрыйце паведамленне нанова.'),
    'Отправка действия не началась. Откройте сообщение заново.': (
        'Sending the action did not begin. Open the message again.', 'No se empezó a enviar la acción. Vuelve a abrir el mensaje.',
        'O envio da ação não começou. Abra a mensagem novamente.', 'L’envoi de l’action n’a pas commencé. Ouvrez à nouveau le message.',
        'Amalni yuborish boshlanmadi. Xabarni qayta oching.', 'Адпраўка дзеяння не пачалася. Адкрыйце паведамленне нанова.'),
    'MAX отклонил действие. Обновите чат и проверьте доступные действия.': (
        'MAX rejected the action. Refresh the chat and check the available actions.',
        'MAX rechazó la acción. Actualiza el chat y comprueba las acciones disponibles.',
        'O MAX recusou a ação. Atualize o chat e confira as ações disponíveis.',
        'MAX a refusé l’action. Actualisez la discussion et vérifiez les actions disponibles.',
        'MAX amalni rad etdi. Chatni yangilang va mavjud amallarni tekshiring.',
        'MAX адхіліў дзеянне. Абнавіце чат і праверце даступныя дзеянні.'),
    'Результат действия неизвестен. Проверьте сообщение перед повторной попыткой.': (
        'The action’s result is unknown. Check the message before trying again.',
        'Se desconoce el resultado de la acción. Comprueba el mensaje antes de volver a intentarlo.',
        'O resultado da ação é desconhecido. Confira a mensagem antes de tentar novamente.',
        'Le résultat de l’action est inconnu. Vérifiez le message avant de réessayer.',
        'Amal natijasi noma’lum. Qayta urinishdan oldin xabarni tekshiring.',
        'Вынік дзеяння невядомы. Праверце паведамленне перад паўторнай спробай.'),
    'Не удалось проверить параметры действия. Обновите чат перед повторной попыткой.': (
        'The action’s parameters could not be verified. Refresh the chat before trying again.',
        'No se pudieron verificar los parámetros de la acción. Actualiza el chat antes de volver a intentarlo.',
        'Não foi possível verificar os parâmetros da ação. Atualize o chat antes de tentar novamente.',
        'Impossible de vérifier les paramètres de l’action. Actualisez la discussion avant de réessayer.',
        'Amal parametrlarini tekshirib bo‘lmadi. Qayta urinishdan oldin chatni yangilang.',
        'Не ўдалося праверыць параметры дзеяння. Абнавіце чат перад паўторнай спробай.'),
    'Не удалось установить результат действия. Проверьте сообщение перед повторной попыткой.': (
        'The action’s result could not be determined. Check the message before trying again.',
        'No se pudo determinar el resultado de la acción. Comprueba el mensaje antes de volver a intentarlo.',
        'Não foi possível determinar o resultado da ação. Confira a mensagem antes de tentar novamente.',
        'Impossible de déterminer le résultat de l’action. Vérifiez le message avant de réessayer.',
        'Amal natijasini aniqlab bo‘lmadi. Qayta urinishdan oldin xabarni tekshiring.',
        'Не ўдалося вызначыць вынік дзеяння. Праверце паведамленне перад паўторнай спробай.'),
    'В Избранном доступно только удаление у себя. Откройте подтверждение заново.': (
        'In Favorites, messages can only be deleted for yourself. Open the confirmation again.',
        'En Favoritos solo puedes eliminar para ti. Vuelve a abrir la confirmación.',
        'Em Favoritos, só é possível excluir para você. Abra a confirmação novamente.',
        'Dans les Favoris, vous pouvez uniquement supprimer pour vous. Ouvrez à nouveau la confirmation.',
        'Tanlanganlarda xabarlarni faqat o‘zingizda o‘chirish mumkin. Tasdiqlash oynasini qayta oching.',
        'У «Выбраным» даступнае толькі выдаленне ў сябе. Адкрыйце пацвярджэнне нанова.'),
}
