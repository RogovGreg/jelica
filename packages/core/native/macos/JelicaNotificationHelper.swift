import Foundation
import UserNotifications

final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list])
    }
}

let arguments = Array(CommandLine.arguments.dropFirst())
let center = UNUserNotificationCenter.current()
let delegate = NotificationDelegate()
center.delegate = delegate

func usage() -> Never {
    FileHandle.standardError.write(Data("usage: JELICA Notification Helper --request-authorization | --title TITLE --body BODY [--occurrence-id ID]\n".utf8))
    exit(2)
}

func waitForCompletion(_ operation: (@escaping (Bool) -> Void) -> Void) -> Int32 {
    let deadline = Date(timeIntervalSinceNow: 10)
    var finished = false
    var success = false
    let lock = NSLock()
    operation { result in
        lock.lock()
        success = result
        finished = true
        lock.unlock()
    }
    while true {
        lock.lock()
        let done = finished
        lock.unlock()
        if done { return success ? 0 : 4 }
        if Date() >= deadline { return 5 }
        RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
    }
}

func authorizationStatus() -> UNAuthorizationStatus {
    var status = UNAuthorizationStatus.notDetermined
    let deadline = Date(timeIntervalSinceNow: 2)
    center.getNotificationSettings { settings in status = settings.authorizationStatus }
    while status == .notDetermined && Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
    }
    return status
}

func authorizeIfNeeded() -> Bool {
    let status = authorizationStatus()
    if status == .notDetermined {
        return waitForCompletion { done in
            center.requestAuthorization(options: [.alert]) { granted, _ in done(granted) }
        } == 0
    }
    return status != .denied
}

func value(after flag: String) -> String? {
    guard let index = arguments.firstIndex(of: flag), index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}

if arguments == ["--request-authorization"] {
    exit(authorizeIfNeeded() ? 0 : 3)
}

guard arguments.contains("--title"), arguments.contains("--body"),
      let title = value(after: "--title"), let body = value(after: "--body"),
      !title.isEmpty, !body.isEmpty, title.utf8.count <= 512, body.utf8.count <= 1024 else { usage() }
let occurrenceID = value(after: "--occurrence-id") ?? UUID().uuidString
guard occurrenceID.range(of: "^[A-Za-z0-9._:-]{1,128}$", options: .regularExpression) != nil else { usage() }
guard authorizeIfNeeded() else { exit(3) }

let content = UNMutableNotificationContent()
content.title = title
content.body = body
content.sound = nil
let request = UNNotificationRequest(identifier: occurrenceID, content: content, trigger: nil)
exit(waitForCompletion { done in center.add(request) { error in done(error == nil) } })
