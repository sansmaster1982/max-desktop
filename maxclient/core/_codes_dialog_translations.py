"""App-owned wording for the read-only MAX verification-message viewer.

Columns: en, es, pt-BR, fr, uz, be. Message bodies and server times remain data.
"""

CODES_DIALOG_TRANSLATIONS = {
    'Сессия недоступна. Закройте окно и откройте его снова.': (
        'The session is unavailable. Close this window and open it again.',
        'La sesión no está disponible. Cierra esta ventana y vuelve a abrirla.',
        'A sessão está indisponível. Feche esta janela e abra-a novamente.',
        'La session est indisponible. Fermez cette fenêtre, puis rouvrez-la.',
        'Sessiya mavjud emas. Oynani yoping va qayta oching.',
        'Сеанс недаступны. Закрыйце гэтае акно і адкрыйце яго зноў.'),
    'Коды подтверждения — MAX': (
        'Verification codes — MAX', 'Códigos de verificación — MAX', 'Códigos de verificação — MAX',
        'Codes de vérification — MAX', 'Tasdiqlash kodlari — MAX', 'Коды пацвярджэння — MAX'),
    'Коды подтверждения': (
        'Verification codes', 'Códigos de verificación', 'Códigos de verificação',
        'Codes de vérification', 'Tasdiqlash kodlari', 'Коды пацвярджэння'),
    'Проверка доступного служебного чата…': (
        'Checking the available service chat…', 'Comprobando el chat de servicio disponible…',
        'Verificando o chat de serviço disponível…', 'Vérification du chat de service disponible…',
        'Mavjud xizmat chati tekshirilmoqda…', 'Праверка даступнага службовага чата…'),
    'Здесь появится доступное сообщение с серверным временем.': (
        'An available message with its server timestamp will appear here.',
        'Aquí aparecerá un mensaje disponible con la hora del servidor.',
        'Uma mensagem disponível com o horário do servidor aparecerá aqui.',
        'Un message disponible avec son horodatage serveur apparaîtra ici.',
        'Bu yerda server vaqti ko‘rsatilgan mavjud xabar paydo bo‘ladi.',
        'Тут з’явіцца даступнае паведамленне з серверным часам.'),
    'Проверить сообщения': (
        'Check messages', 'Comprobar mensajes', 'Verificar mensagens',
        'Vérifier les messages', 'Xabarlarni tekshirish', 'Праверыць паведамленні'),
    'Копировать последнее сообщение': (
        'Copy the latest message', 'Copiar el último mensaje', 'Copiar a última mensagem',
        'Copier le dernier message', 'Oxirgi xabarni nusxalash', 'Скапіяваць апошняе паведамленне'),
    'Доступные сообщения. Сверьте время сообщения со своим запросом входа.': (
        'Available messages. Check the message time against your sign-in request.',
        'Mensajes disponibles. Compara la hora del mensaje con tu solicitud de inicio de sesión.',
        'Mensagens disponíveis. Compare o horário da mensagem com sua solicitação de acesso.',
        'Messages disponibles. Comparez l’heure du message à celle de votre demande de connexion.',
        'Mavjud xabarlar. Xabar vaqtini kirish so‘rovingiz vaqti bilan solishtiring.',
        'Даступныя паведамленні. Зверце час паведамлення са сваім запытам на ўваход.'),
    'Сообщения с подтверждённым серверным временем не найдены.': (
        'No messages with a confirmed server timestamp were found.',
        'No se encontraron mensajes con una hora del servidor confirmada.',
        'Não foram encontradas mensagens com horário do servidor confirmado.',
        'Aucun message avec un horodatage serveur confirmé n’a été trouvé.',
        'Server vaqti tasdiqlangan xabarlar topilmadi.',
        'Паведамленні з пацверджаным серверным часам не знойдзены.'),
    'Чат «Коды подтверждения» не доступен этой сессии. Если настроено основное устройство MAX, проверьте код на нём или дождитесь SMS на номер аккаунта.': (
        'The “Verification codes” chat is unavailable in this session. If you have set up a primary MAX device, check the code there or wait for an SMS to the account’s phone number.',
        'El chat «Códigos de verificación» no está disponible en esta sesión. Si tienes un dispositivo principal de MAX configurado, consulta el código allí o espera un SMS al número de la cuenta.',
        'O chat “Códigos de verificação” está indisponível nesta sessão. Se você configurou um dispositivo principal do MAX, confira o código nele ou aguarde um SMS no número da conta.',
        'Le chat « Codes de vérification » est indisponible dans cette session. Si un appareil principal MAX est configuré, consultez le code sur cet appareil ou attendez un SMS au numéro du compte.',
        '«Tasdiqlash kodlari» chati bu sessiyada mavjud emas. Agar MAX asosiy qurilmasi sozlangan bo‘lsa, kodni o‘sha qurilmada tekshiring yoki hisobingizning telefon raqamiga SMS kelishini kuting.',
        'Чат «Коды пацвярджэння» недаступны ў гэтым сеансе. Калі наладжана асноўная прылада MAX, праверце код на ёй або дачакайцеся SMS на нумар акаўнта.'),
    'Служебный чат найден, но доступного сообщения с кодом пока нет. Если код скрыт для этого устройства, проверьте основное устройство MAX или SMS.': (
        'The service chat was found, but no message containing a code is available yet. If the code is hidden on this device, check your primary MAX device or SMS.',
        'Se encontró el chat de servicio, pero aún no hay ningún mensaje disponible con un código. Si el código está oculto en este dispositivo, revisa tu dispositivo principal de MAX o los SMS.',
        'O chat de serviço foi encontrado, mas ainda não há mensagem disponível com um código. Se o código estiver oculto neste dispositivo, confira seu dispositivo principal do MAX ou o SMS.',
        'Le chat de service a été trouvé, mais aucun message contenant un code n’est encore disponible. Si le code est masqué sur cet appareil, consultez votre appareil principal MAX ou vos SMS.',
        'Xizmat chati topildi, ammo kodli xabar hozircha mavjud emas. Agar kod bu qurilmada yashirilgan bo‘lsa, asosiy MAX qurilmangizni yoki SMSni tekshiring.',
        'Службовы чат знойдзены, але даступнага паведамлення з кодам пакуль няма. Калі код схаваны для гэтай прылады, праверце асноўную прыладу MAX або SMS.'),
    'Не удалось обновить служебный чат. Проверьте соединение и повторите проверку.': (
        'Could not refresh the service chat. Check your connection and try again.',
        'No se pudo actualizar el chat de servicio. Comprueba la conexión y vuelve a intentarlo.',
        'Não foi possível atualizar o chat de serviço. Verifique a conexão e tente novamente.',
        'Impossible d’actualiser le chat de service. Vérifiez votre connexion et réessayez.',
        'Xizmat chatini yangilab bo‘lmadi. Ulanishni tekshiring va qayta urinib ko‘ring.',
        'Не ўдалося абнавіць службовы чат. Праверце злучэнне і паўтарыце праверку.'),
}
