public with sharing class AccountTriggerHandler {

    private static boolean bypassTrigger = false;

    public static void beforeInsert(List<Account> newAccounts) {
        preventDuplicateAccounts(newAccounts, null);
    }

    public static void beforeUpdate(List<Account> newAccounts, Map<Id, Account> oldAccountMap) {
        preventDuplicateAccounts(newAccounts, oldAccountMap);
    }

    private static void preventDuplicateAccounts(List<Account> accounts, Map<Id, Account> oldAccountMap) {
        if (bypassTrigger) return;

        Set<String> phoneNumbers = new Set<String>();
        for (Account acc : accounts) {
            if (acc.Phone != null) {
                // Only consider phone numbers that are new or have changed
                if (oldAccountMap == null || (oldAccountMap.get(acc.Id) != null && oldAccountMap.get(acc.Id).Phone != acc.Phone) || acc.Id == null) {
                    phoneNumbers.add(acc.Phone);
                }
            }
        }

        if (!phoneNumbers.isEmpty()) {
            List<Account> existingAccounts = [
                SELECT Id, Phone
                FROM Account
                WHERE Phone IN :phoneNumbers
                WITH SECURITY_ENFORCED
            ];

            Map<String, Id> existingPhonesMap = new Map<String, Id>();
            for (Account acc : existingAccounts) {
                existingPhonesMap.put(acc.Phone, acc.Id);
            }

            for (Account acc : accounts) {
                if (acc.Phone != null && existingPhonesMap.containsKey(acc.Phone)) {
                    // If it's an update, ensure it's not the same record itself
                    if (oldAccountMap != null && existingPhonesMap.get(acc.Phone) == acc.Id) {
                        continue; // Same record, same phone, no duplicate
                    }
                    acc.Phone.addError('An Account with this phone number already exists: ' + acc.Phone);
                }
            }
        }
    }

    public static void setBypassTrigger(boolean bypass) {
        bypassTrigger = bypass;
    }
}