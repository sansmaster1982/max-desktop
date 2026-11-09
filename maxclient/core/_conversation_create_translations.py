"""Client-owned creation form text; these are not official MAX translations.

Columns: en, es, pt-BR, fr, uz, be. Names and descriptions remain user data.
"""

CONVERSATION_CREATE_TRANSLATIONS = {
    # The inherited French entry contains Russian. Preserve the other columns.
    'Не создавать': (
        "Don't create", 'No crear', 'Não criar', 'Ne pas créer', 'Yaratmaslik', 'Не ствараць'),
    'Результат создания': (
        'Creation result', 'Resultado de la creación', 'Resultado da criação',
        'Résultat de la création', 'Yaratish natijasi', 'Вынік стварэння'),
    'Проверить результат': (
        'Check result', 'Comprobar resultado', 'Verificar resultado',
        'Vérifier le résultat', 'Natijani tekshirish', 'Праверыць вынік'),
    'Открыть созданный чат': (
        'Open created chat', 'Abrir el chat creado', 'Abrir conversa criada',
        'Ouvrir la conversation créée', 'Yaratilgan chatni ochish', 'Адкрыць створаны чат'),
    'Поиск участников по имени или номеру': (
        'Find participants by name or phone number', 'Buscar participantes por nombre o número',
        'Buscar participantes por nome ou número', 'Rechercher des participants par nom ou numéro',
        'Ishtirokchilarni ism yoki raqam bo‘yicha qidirish', 'Пошук удзельнікаў па імені або нумары'),
    'Название канала': (
        'Channel name', 'Nombre del canal', 'Nome do canal',
        'Nom du canal', 'Kanal nomi', 'Назва канала'),
    'Выбрано участников: {count}': (
        'Selected participants: {count}', 'Participantes seleccionados: {count}',
        'Participantes selecionados: {count}', 'Participants sélectionnés : {count}',
        'Tanlangan ishtirokchilar: {count}', 'Выбрана ўдзельнікаў: {count}'),
    'Проверка результата…': (
        'Checking result…', 'Comprobando resultado…', 'Verificando resultado…',
        'Vérification du résultat…', 'Natija tekshirilmoqda…', 'Праверка выніку…'),
    'Создание…': (
        'Creating…', 'Creando…', 'Criando…', 'Création…', 'Yaratilmoqda…', 'Стварэнне…'),
    'Сессия изменилась. Откройте создание заново.': (
        'The session has changed. Reopen the creation form.',
        'La sesión ha cambiado. Vuelve a abrir el formulario de creación.',
        'A sessão mudou. Abra novamente o formulário de criação.',
        'La session a changé. Rouvrez le formulaire de création.',
        'Seans o‘zgardi. Yaratish oynasini qayta oching.',
        'Сесія змянілася. Адкрыйце форму стварэння нанова.'),
    'Не удалось загрузить контакты. Откройте создание группы заново.': (
        'Could not load contacts. Reopen the group creation form.',
        'No se pudieron cargar los contactos. Vuelve a abrir el formulario para crear el grupo.',
        'Não foi possível carregar os contatos. Abra novamente o formulário para criar o grupo.',
        'Impossible de charger les contacts. Rouvrez le formulaire de création du groupe.',
        'Kontaktlarni yuklab bo‘lmadi. Guruh yaratish oynasini qayta oching.',
        'Не ўдалося загрузіць кантакты. Адкрыйце форму стварэння групы нанова.'),
    'Не удалось проверить параметры создания. Откройте создание заново.': (
        'Could not validate the creation details. Reopen the creation form.',
        'No se pudieron comprobar los datos de creación. Vuelve a abrir el formulario.',
        'Não foi possível validar os dados de criação. Abra novamente o formulário.',
        'Impossible de vérifier les paramètres de création. Rouvrez le formulaire.',
        'Yaratish parametrlarini tekshirib bo‘lmadi. Yaratish oynasini qayta oching.',
        'Не ўдалося праверыць параметры стварэння. Адкрыйце форму стварэння нанова.'),
    'Результат создания пока не подтверждён. Повторное создание заблокировано.': (
        'Creation has not yet been confirmed. Creating again is blocked.',
        'La creación aún no está confirmada. No se permite volver a crear.',
        'A criação ainda não foi confirmada. Criar novamente está bloqueado.',
        'La création n’est pas encore confirmée. Une nouvelle création est bloquée.',
        'Yaratish natijasi hali tasdiqlanmadi. Qayta yaratish bloklangan.',
        'Вынік стварэння пакуль не пацверджаны. Паўторнае стварэнне заблакавана.'),
    'MAX не подтвердил операцию. Проверьте данные и подключение.': (
        'MAX did not confirm the operation. Check the details and connection.',
        'MAX no confirmó la operación. Comprueba los datos y la conexión.',
        'O MAX não confirmou a operação. Verifique os dados e a conexão.',
        'MAX n’a pas confirmé l’opération. Vérifiez les données et la connexion.',
        'MAX amalni tasdiqlamadi. Ma’lumotlar va ulanishni tekshiring.',
        'MAX не пацвердзіў аперацыю. Праверце даныя і злучэнне.'),
    'Не удалось подтвердить результат. Повторное создание заблокировано.': (
        'Could not confirm the result. Creating again is blocked.',
        'No se pudo confirmar el resultado. No se permite volver a crear.',
        'Não foi possível confirmar o resultado. Criar novamente está bloqueado.',
        'Impossible de confirmer le résultat. Une nouvelle création est bloquée.',
        'Natijani tasdiqlab bo‘lmadi. Qayta yaratish bloklangan.',
        'Не ўдалося пацвердзіць вынік. Паўторнае стварэнне заблакавана.'),
    'Предыдущая операция создания ещё не подтверждена. Новое создание заблокировано.': (
        'The previous creation is still unconfirmed. A new creation is blocked.',
        'La creación anterior sigue sin confirmarse. No se permite crear otra.',
        'A criação anterior ainda não foi confirmada. Uma nova criação está bloqueada.',
        'La création précédente n’est toujours pas confirmée. Une nouvelle création est bloquée.',
        'Oldingi yaratish amali hali tasdiqlanmadi. Yangi yaratish bloklangan.',
        'Папярэдняя аперацыя стварэння яшчэ не пацверджана. Новае стварэнне заблакавана.'),
    'Канал создан, но описание не сохранено.': (
        'The channel was created, but its description was not saved.',
        'El canal se ha creado, pero no se ha guardado la descripción.',
        'O canal foi criado, mas a descrição não foi salva.',
        'Le canal a été créé, mais sa description n’a pas été enregistrée.',
        'Kanal yaratildi, ammo tavsifi saqlanmadi.',
        'Канал створаны, але апісанне не захавана.'),
    'Канал создан, но сохранение описания не подтверждено. Можно открыть канал или проверить результат.': (
        'The channel was created, but saving its description is unconfirmed. You can open the channel or check the result.',
        'El canal se ha creado, pero no se ha confirmado que la descripción se haya guardado. Puedes abrir el canal o comprobar el resultado.',
        'O canal foi criado, mas o salvamento da descrição não foi confirmado. Você pode abrir o canal ou verificar o resultado.',
        'Le canal a été créé, mais l’enregistrement de sa description n’est pas confirmé. Vous pouvez ouvrir le canal ou vérifier le résultat.',
        'Kanal yaratildi, ammo tavsif saqlangani tasdiqlanmadi. Kanalni ochishingiz yoki natijani tekshirishingiz mumkin.',
        'Канал створаны, але захаванне апісання не пацверджана. Можна адкрыць канал або праверыць вынік.'),
    'Канал создан, но сохранение описания не подтверждено.': (
        'The channel was created, but saving its description is unconfirmed.',
        'El canal se ha creado, pero no se ha confirmado que la descripción se haya guardado.',
        'O canal foi criado, mas o salvamento da descrição não foi confirmado.',
        'Le canal a été créé, mais l’enregistrement de sa description n’est pas confirmé.',
        'Kanal yaratildi, ammo tavsif saqlangani tasdiqlanmadi.',
        'Канал створаны, але захаванне апісання не пацверджана.'),
    'MAX не подтвердил создание.': (
        'MAX did not confirm creation.', 'MAX no confirmó la creación.',
        'O MAX não confirmou a criação.', 'MAX n’a pas confirmé la création.',
        'MAX yaratilganini tasdiqlamadi.', 'MAX не пацвердзіў стварэнне.'),
    'Результат операции неизвестен. Повторное создание заблокировано.': (
        'The outcome is unknown. Creating again is blocked.',
        'Se desconoce el resultado de la operación. No se permite volver a crear.',
        'O resultado da operação é desconhecido. Criar novamente está bloqueado.',
        'Le résultat de l’opération est inconnu. Une nouvelle création est bloquée.',
        'Amal natijasi noma’lum. Qayta yaratish bloklangan.',
        'Вынік аперацыі невядомы. Паўторнае стварэнне заблакавана.'),
    'Не удалось подтвердить запуск операции. Повторное создание заблокировано.': (
        'Could not confirm that the operation started. Creating again is blocked.',
        'No se pudo confirmar el inicio de la operación. No se permite volver a crear.',
        'Não foi possível confirmar o início da operação. Criar novamente está bloqueado.',
        'Impossible de confirmer le démarrage de l’opération. Une nouvelle création est bloquée.',
        'Amal boshlanganini tasdiqlab bo‘lmadi. Qayta yaratish bloklangan.',
        'Не ўдалося пацвердзіць пачатак аперацыі. Паўторнае стварэнне заблакавана.'),
    'Неверные ограничения MAX': (
        'Invalid MAX limits', 'Límites de MAX no válidos', 'Limites inválidos do MAX',
        'Limites MAX non valides', 'MAX cheklovlari noto‘g‘ri', 'Няправільныя абмежаванні MAX'),
    'MAX вернул неверные ограничения создания': (
        'MAX returned invalid creation limits', 'MAX devolvió límites de creación no válidos',
        'O MAX retornou limites de criação inválidos', 'MAX a renvoyé des limites de création non valides',
        'MAX noto‘g‘ri yaratish cheklovlarini qaytardi', 'MAX вярнуў няправільныя абмежаванні стварэння'),
    'Укажите тип, название и описание': (
        'Specify the type, name and description', 'Indica el tipo, el nombre y la descripción',
        'Informe o tipo, o nome e a descrição', 'Indiquez le type, le nom et la description',
        'Tur, nom va tavsifni kiriting', 'Укажыце тып, назву і апісанне'),
    'Выберите уникальные контакты MAX': (
        'Select distinct MAX contacts', 'Selecciona contactos de MAX sin duplicados',
        'Selecione contatos do MAX sem duplicatas', 'Sélectionnez des contacts MAX distincts',
        'Takrorlanmaydigan MAX kontaktlarini tanlang', 'Выберыце непаўторныя кантакты MAX'),
    'Создание канала не добавляет подписчиков': (
        'Creating a channel does not add subscribers', 'Crear un canal no añade suscriptores',
        'Criar um canal não adiciona inscritos', 'La création d’un canal n’ajoute pas d’abonnés',
        'Kanal yaratish obunachilarni qo‘shmaydi', 'Стварэнне канала не дадае падпісчыкаў'),
    'Описание доступно при создании канала': (
        'A description is available when creating a channel',
        'La descripción está disponible al crear un canal',
        'A descrição está disponível ao criar um canal',
        'La description est disponible lors de la création d’un canal',
        'Tavsif kanal yaratishda mavjud', 'Апісанне даступнае пры стварэнні канала'),
    'Название должно содержать от 1 до {limit} символов': (
        'The name must contain between 1 and {limit} characters',
        'El nombre debe tener entre 1 y {limit} caracteres',
        'O nome deve conter de 1 a {limit} caracteres',
        'Le nom doit contenir entre 1 et {limit} caractères',
        'Nom 1 tadan {limit} tagacha belgidan iborat bo‘lishi kerak',
        'Назва павінна змяшчаць ад 1 да {limit} сімвалаў'),
    'Описание: не более {limit} символов': (
        'Description: no more than {limit} characters',
        'Descripción: como máximo {limit} caracteres',
        'Descrição: no máximo {limit} caracteres',
        'Description : {limit} caractères maximum',
        'Tavsif: {limit} tadan ko‘p bo‘lmagan belgi',
        'Апісанне: не больш за {limit} сімвалаў'),
    'Можно выбрать не более {limit} участников': (
        'You can select no more than {limit} participants',
        'Puedes seleccionar como máximo {limit} participantes',
        'Você pode selecionar no máximo {limit} participantes',
        'Vous pouvez sélectionner {limit} participants au maximum',
        'Ko‘pi bilan {limit} ta ishtirokchini tanlash mumkin',
        'Можна выбраць не больш за {limit} удзельнікаў'),
}

__all__ = ['CONVERSATION_CREATE_TRANSLATIONS']
