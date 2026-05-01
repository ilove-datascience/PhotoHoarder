import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, random_split, Dataset

import torchvision.transforms as transforms
from torchvision.datasets import ImageFolder

import timm

import torch
print(torch.cuda.is_available())
print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "No GPU")
class TransformedDataset(Dataset):
    def __init__(self, subset, transform):
        self.subset = subset
        self.transform = transform

    def __len__(self):
        return len(self.subset)

    def __getitem__(self, idx):
        image, label = self.subset[idx]
        if self.transform:
            image = self.transform(image)
        return image, label
    
# ========================
# Config
# ========================
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
BATCH_SIZE = 32
EPOCHS = 23
LR = 0.0006
IMG_SIZE = 224
SEED= 6767

print(f"Using device: {DEVICE}")
print(f"Batch size: {BATCH_SIZE}, epochs: {EPOCHS}, image size: {IMG_SIZE}")

# ========================
# Transforms
# ========================
train_transforms = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.RandomHorizontalFlip(),
    transforms.RandomRotation(10),
    transforms.ToTensor(),
])

val_transforms = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
])
# ========================
# Dataset & Loader
# ========================
print("Loading dataset from data/train...")
full_dataset = ImageFolder(r"C:\Users\Jacobs laptop\PhotoHoarder\ml\data\train")
test_dataset = ImageFolder(r"C:\Users\Jacobs laptop\PhotoHoarder\ml\data\test", transform=val_transforms)

train_size = int(0.7 * len(full_dataset))
val_size = len(full_dataset) - train_size

print(f"Found {len(full_dataset)} images across {len(full_dataset.classes)} classes")
print(f"Split sizes -> train: {train_size}, val: {val_size}")

generator = torch.Generator().manual_seed(SEED)

train_dataset, val_dataset = random_split(
    full_dataset,
    [train_size, val_size],
    generator=generator
)

train_dataset = TransformedDataset(train_dataset, train_transforms)
val_dataset = TransformedDataset(val_dataset, val_transforms)

train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True)
val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False)
test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False)

print(f"Train batches: {len(train_loader)}, val batches: {len(val_loader)}")
print(f"Holdout test images: {len(test_dataset)}, test batches: {len(test_loader)}")

if full_dataset.classes != test_dataset.classes:
    print("Warning: train and test class names do not match")

# ========================
# Model
# ========================
print("Building model: mobilenetv3_small_100")
model = timm.create_model(
    "mobilenetv3_small_100",  # lightweight
    pretrained=True,
    num_classes=2
).to(DEVICE)
print("Model ready")

# ========================
# Loss & Optimizer
# ========================
criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=LR)

# ========================
# Training Loop
# ========================
for epoch in range(EPOCHS):
    print(f"Starting epoch {epoch + 1}/{EPOCHS}")
    model.train()
    train_loss = 0

    for images, labels in train_loader:
        images, labels = images.to(DEVICE), labels.to(DEVICE)

        optimizer.zero_grad()

        outputs = model(images)
        loss = criterion(outputs, labels)

        loss.backward()
        optimizer.step()

        train_loss += loss.item()

    avg_train_loss = train_loss / len(train_loader)

    # ========================
    # Validation
    # ========================
    model.eval()
    correct = 0
    total = 0
    val_loss = 0

    with torch.no_grad():
        for images, labels in val_loader:
            images, labels = images.to(DEVICE), labels.to(DEVICE)

            outputs = model(images)
            loss = criterion(outputs, labels)
            val_loss += loss.item()

            _, predicted = torch.max(outputs, 1)
            correct += (predicted == labels).sum().item()
            total += labels.size(0)

    avg_val_loss = val_loss / len(val_loader)
    accuracy = correct / total

    print(f"Epoch [{epoch+1}/{EPOCHS}]")
    print(f"Train Loss: {avg_train_loss:.4f}")
    print(f"Val Loss: {avg_val_loss:.4f} | Accuracy: {accuracy:.4f}")
    print("-" * 40)

# ========================
# Save model
# ========================
print("Evaluating holdout set from data/test...")
model.eval()
test_correct = 0
test_total = 0
test_loss = 0
class_correct = [0 for _ in full_dataset.classes]
class_total = [0 for _ in full_dataset.classes]
misclassified_samples = []
test_sample_offset = 0

with torch.no_grad():
    for images, labels in test_loader:
        images, labels = images.to(DEVICE), labels.to(DEVICE)

        outputs = model(images)
        probs = torch.softmax(outputs, dim=1)
        loss = criterion(outputs, labels)
        test_loss += loss.item()

        _, predicted = torch.max(outputs, 1)
        test_correct += (predicted == labels).sum().item()
        test_total += labels.size(0)

        for batch_idx, (label, pred) in enumerate(zip(labels, predicted)):
            label_idx = label.item()
            pred_idx = pred.item()
            class_total[label_idx] += 1
            if pred_idx == label_idx:
                class_correct[label_idx] += 1
            else:
                sample_idx = test_sample_offset + batch_idx
                sample_path, _ = test_dataset.samples[sample_idx]
                pred_conf = probs[batch_idx, pred_idx].item()
                true_conf = probs[batch_idx, label_idx].item()
                misclassified_samples.append(
                    (
                        sample_path,
                        full_dataset.classes[label_idx],
                        full_dataset.classes[pred_idx],
                        pred_conf,
                        true_conf,
                    )
                )

        test_sample_offset += labels.size(0)

avg_test_loss = test_loss / len(test_loader) if len(test_loader) > 0 else 0.0
test_accuracy = (test_correct / test_total) if test_total > 0 else 0.0

print("Holdout Test Results")
print(f"Test Loss: {avg_test_loss:.4f}")
print(f"Test Accuracy: {test_accuracy:.4f}")

for idx, class_name in enumerate(full_dataset.classes):
    class_acc = (class_correct[idx] / class_total[idx]) if class_total[idx] > 0 else 0.0
    print(f"Class '{class_name}': {class_correct[idx]}/{class_total[idx]} correct ({class_acc:.4f})")

print(f"Misclassified files: {len(misclassified_samples)}")
if misclassified_samples:
    for sample_path, true_label, pred_label, pred_conf, true_conf in misclassified_samples:
        print(
            f"- {sample_path} | true={true_label}, predicted={pred_label}, "
            f"pred_conf={pred_conf:.4f}, true_conf={true_conf:.4f}"
        )

print("Saving model to model_lowerlr.pth")
torch.save(model.state_dict(), "model_lowerlr.pth")
print("Done")