import os

# Testy zakladaja profil GPU (jak na docelowym sprzecie), niezaleznie od maszyny, na ktorej dzialaja; testy trybu CPU
# nadpisuja te zmienna same.
os.environ.setdefault("LECTURENOTER_DEVICE", "cuda")
